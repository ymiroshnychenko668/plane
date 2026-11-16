"""Real PostgreSQL capture, source lifecycle and three-valued rolling windows."""

from contextlib import contextmanager
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

from plane.db.models import (
    ActivityAssessment,
    ActivityAssessmentRevision,
    ActivityCoverage,
    ActivitySourceEvent,
    DeployBoard,
    Intake,
    IntakeIssue,
    Issue,
    IssueComment,
    Project,
    ProjectMember,
)
from plane.utils.meaningful_activity import POLICY, SOURCE_SCHEMA, annotate_activity, pending_events
from plane.utils.activity_writer import task_after_commit

pytestmark = [pytest.mark.contract, pytest.mark.django_db(transaction=True)]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    settings.APP_BASE_URL = "https://plane.example.invalid"
    settings.WEB_URL = "https://plane.example.invalid"
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Activity", identifier="ACT")
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20)
    return project


@pytest.fixture
def issue(project):
    return Issue.objects.create(project=project, workspace=project.workspace, name="Activity fixture")


@contextmanager
def writer(user, producer="native", mutation=None):
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('plane.activity_writer', %s, true), "
                "set_config('plane.activity_producer', %s, true), "
                "set_config('plane.activity_mutation', %s, true)",
                [str(user.id), producer, mutation or str(uuid4())],
            )
        yield


def comment(issue, user, text="Found the cause: lock ordering was reversed.", producer="native"):
    with writer(user, producer):
        row = IssueComment.objects.create(
            issue=issue, project=issue.project, workspace=issue.workspace, actor=user, comment_html=f"<p>{text}</p>"
        )
    return row, ActivitySourceEvent.objects.get(source_kind="comment", source_id=row.id)


def age_coverage(issue, now, days=20):
    ActivityCoverage.objects.filter(issue=issue).update(
        captured_from=now - timedelta(days=days),
        known_existing_at=now - timedelta(days=days),
        native_created_at=None,
    )


def evaluate(issue, now, days=7):
    return annotate_activity(
        Issue.objects.filter(pk=issue.id),
        evaluated_at=now,
        days=days,
        policy_version=POLICY,
        source_schema_version=SOURCE_SCHEMA,
    ).get()


def assess(event, decision="meaningful"):
    return ActivityAssessment.objects.create(
        event=event,
        policy_version=POLICY,
        decision=decision,
        category="investigation" if decision == "meaningful" else "routine",
        rationale="Concrete result.",
    )


def test_creation_is_baseline_and_authenticated_native_age(project, create_user):
    with writer(create_user):
        issue = Issue.objects.create(
            project=project, workspace=project.workspace, name="New", description_html="<p>Do work</p>"
        )
    event = ActivitySourceEvent.objects.get(issue=issue)
    assert event.is_baseline and event.time_quality == "native"
    assert event.authenticated_writer_id == create_user.id and event.occurred_at is not None
    assert ActivityCoverage.objects.get(issue=issue).native_created_at is not None
    assert evaluate(issue, timezone.now()).activity_match is False
    assert not pending_events().filter(issue=issue).exists()


def test_uninstrumented_bulk_update_is_captured_but_not_given_native_time(issue):
    Issue.objects.filter(pk=issue.id).update(description_html="<p>Found a regression.</p>")
    events = ActivitySourceEvent.objects.filter(issue=issue, is_baseline=False)
    event = events.get()
    assert event.time_quality == "unattributed" and event.occurred_at is None
    assert pending_events().filter(pk=event.pk).exists()


def test_metadata_and_timestamp_updates_do_not_generate_activity(issue):
    before = ActivitySourceEvent.objects.filter(issue=issue).count()
    Issue.objects.filter(pk=issue.id).update(name="Renamed", priority="urgent", updated_at=timezone.now())
    assert ActivitySourceEvent.objects.filter(issue=issue).count() == before


def test_transaction_coalesces_first_before_final_after_and_same_request_cannot_rewrite_commit(issue, create_user):
    mutation = str(uuid4())
    with writer(create_user, mutation=mutation):
        Issue.objects.filter(pk=issue.id).update(description_html="<p>Intermediate</p>")
        Issue.objects.filter(pk=issue.id).update(description_html="<p>Final</p>")
    event = ActivitySourceEvent.objects.get(issue=issue, is_baseline=False)
    assert event.before_content["html"] == "<p></p>" and event.source_content["html"] == "<p>Final</p>"
    with writer(create_user, mutation=mutation):
        Issue.objects.filter(pk=issue.id).update(description_html="<p>Later</p>")
    event.refresh_from_db()
    assert event.source_content["html"] == "<p>Final</p>"
    assert ActivitySourceEvent.objects.filter(issue=issue, is_baseline=False).count() == 2


def test_net_noop_and_rollback_do_not_create_progress(issue, create_user):
    with writer(create_user):
        Issue.objects.filter(pk=issue.id).update(description_html="<p>Intermediate</p>")
        Issue.objects.filter(pk=issue.id).update(description_html="<p></p>")
    assert not pending_events().filter(issue=issue).exists()
    with pytest.raises(RuntimeError), writer(create_user):
        Issue.objects.filter(pk=issue.id).update(description_html="<p>Rolled back</p>")
        raise RuntimeError("rollback")
    assert not ActivitySourceEvent.objects.filter(source_content__html="<p>Rolled back</p>").exists()


def test_late_import_fields_downgrade_complete_logical_write(project, create_user):
    with writer(create_user):
        issue = Issue.objects.create(project=project, workspace=project.workspace, name="Imported")
        Issue.objects.filter(pk=issue.id).update(created_at=timezone.now() - timedelta(days=90))
    event = ActivitySourceEvent.objects.get(issue=issue)
    assert event.time_quality == "import" and event.occurred_at is None
    assert ActivityCoverage.objects.get(issue=issue).native_created_at is None
    assert evaluate(issue, timezone.now()).activity_match is None


@pytest.mark.parametrize("mutation", ["soft_delete", "access", "hard_delete"])
def test_source_loss_immediately_redacts_snapshots_and_rationale(issue, create_user, mutation):
    row, event = comment(issue, create_user)
    assessment = assess(event)
    history = ActivityAssessmentRevision.objects.create(
        assessment=assessment,
        revision=1,
        decision="meaningful",
        category="investigation",
        rationale="Sensitive explanation",
    )
    if mutation == "soft_delete":
        IssueComment.objects.filter(pk=row.id).delete()
    elif mutation == "access":
        IssueComment.objects.filter(pk=row.id).update(access="EXTERNAL")
    else:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM issue_comments WHERE id = %s", [row.id])
    event.refresh_from_db()
    assessment.refresh_from_db()
    history.refresh_from_db()
    assert event.redacted and event.source_content == event.before_content == {}
    assert assessment.rationale == history.rationale == ""
    assert assessment.decision == "needs_review"


def test_issue_soft_delete_redacts_comment_evidence(issue, create_user):
    _, event = comment(issue, create_user)
    Issue.objects.filter(pk=issue.id).update(deleted_at=timezone.now())
    event.refresh_from_db()
    assert event.redacted and event.source_content == {}


def test_stale_is_strictly_more_than_x_days_and_assessment_time_is_not_activity(issue, create_user):
    _, event = comment(issue, create_user)
    now = timezone.now()
    age_coverage(issue, now)
    cutoff = now - timedelta(days=7)
    ActivitySourceEvent.objects.filter(pk=event.id).update(occurred_at=cutoff)
    assessment = assess(event)
    assert evaluate(issue, now).activity_match is False
    ActivitySourceEvent.objects.filter(pk=event.id).update(occurred_at=cutoff - timedelta(microseconds=1))
    result = evaluate(issue, now)
    assert result.activity_match is True
    assert result.last_meaningful_at < assessment.assessed_at - timedelta(days=6)


def test_pending_is_unknown_routine_ping_is_not_activity(issue, create_user):
    _, event = comment(issue, create_user, "ping")
    now = timezone.now()
    age_coverage(issue, now)
    assert evaluate(issue, now).activity_match is None
    assess(event, "routine")
    result = evaluate(issue, now)
    assert result.activity_match is True and result.last_meaningful_at is None


def test_unknown_event_time_blocks_only_relevant_window(issue, create_user):
    _, event = comment(issue, create_user, producer="import")
    now = timezone.now()
    age_coverage(issue, now)
    assess(event)
    assert evaluate(issue, now).activity_match is None
    ActivitySourceEvent.objects.filter(pk=event.id).update(observed_at=now - timedelta(days=8))
    assert evaluate(issue, now).activity_match is True


def test_recent_meaningful_has_false_result_but_new_pending_still_in_project_queue(issue, create_user):
    _, event = comment(issue, create_user)
    assess(event)
    _, pending = comment(issue, create_user, "More evidence to classify")
    assert evaluate(issue, timezone.now()).activity_match is False
    assert pending_events().filter(pk=pending.pk).exists()


def test_old_unknown_history_stops_blocking_after_continuous_window(issue):
    now = timezone.now()
    assert evaluate(issue, now).activity_match is None
    age_coverage(issue, now)
    assert evaluate(issue, now).activity_match is True


def test_missing_capture_hook_fails_closed(issue):
    now = timezone.now()
    age_coverage(issue, now)
    with transaction.atomic():
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE issue_comments DISABLE TRIGGER plane_activity_comment_capture")
        result = evaluate(issue, now)
        assert result.activity_match is None and result.activity_reason == "capture_unavailable"
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE issue_comments ENABLE TRIGGER plane_activity_comment_capture")


def test_api_comment_writer_records_authenticated_actor_and_import_provenance(api_key_client, issue, create_user):
    path = f"/api/v1/workspaces/{issue.workspace.slug}/projects/{issue.project_id}/work-items/{issue.id}/comments/"
    response = api_key_client.post(path, {"comment_html": "<p>A concrete investigation result.</p>"}, format="json")
    assert response.status_code == 201, response.data
    event = ActivitySourceEvent.objects.get(source_kind="comment", source_id=response.data["id"])
    assert event.time_quality == "native" and event.authenticated_writer_id == create_user.id
    response = api_key_client.post(
        path,
        {
            "comment_html": "<p>An imported historical comment.</p>",
            "created_at": (timezone.now() - timedelta(days=30)).isoformat(),
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    event = ActivitySourceEvent.objects.get(source_kind="comment", source_id=response.data["id"])
    assert event.time_quality == "import" and event.occurred_at is None


def test_native_skip_activity_does_not_skip_transactional_capture(session_client, issue):
    path = f"/api/workspaces/{issue.workspace.slug}/projects/{issue.project_id}/issues/{issue.id}/"
    response = session_client.patch(
        path,
        {"description_html": "<p>Result with legacy activity suppressed.</p>", "skip_activity": True},
        format="json",
    )
    assert response.status_code == 204, response.data
    event = ActivitySourceEvent.objects.get(issue=issue, is_baseline=False)
    assert event.time_quality == "import" and event.occurred_at is None
    assert "Result with legacy" in event.source_content["html"]


def test_new_native_edits_of_imported_sources_have_native_time(issue, create_user):
    Issue.objects.filter(pk=issue.id).update(external_source="legacy", external_id="old-123")
    with writer(create_user):
        Issue.objects.filter(pk=issue.id).update(description_html="<p>New finding after migration</p>")
    event = ActivitySourceEvent.objects.filter(issue=issue, is_baseline=False).get()
    assert event.time_quality == "native" and event.occurred_at is not None
    with writer(create_user, "import"):
        row = IssueComment.objects.create(
            issue=issue,
            project=issue.project,
            workspace=issue.workspace,
            external_source="legacy",
            external_id="comment-123",
            comment_html="<p>Old comment</p>",
        )
    with writer(create_user):
        IssueComment.objects.filter(pk=row.id).update(comment_html="<p>New concrete result</p>")
    event = ActivitySourceEvent.objects.filter(source_id=row.id).order_by("-source_revision").first()
    assert event.time_quality == "native" and event.occurred_at is not None


@pytest.mark.parametrize(
    "params",
    [
        {"meaningful_activity__older_than_days": "7"},
        {"assignee_id__is_empty": "true"},
        {"rich_filters": '{"and":[{"meaningful_activity__older_than_days":7}]}'},
    ],
)
def test_generic_api_lists_cannot_silently_ignore_view_predicates(api_key_client, issue, params):
    path = f"/api/v1/workspaces/{issue.workspace.slug}/projects/{issue.project_id}/work-items/"
    response = api_key_client.get(path, params)
    assert response.status_code == 400, response.data


def test_public_comment_writer_uses_board_project_and_cannot_write_foreign_source(session_client, issue, create_user):
    board = DeployBoard.objects.create(
        workspace=issue.workspace,
        entity_identifier=issue.project_id,
        entity_name="project",
        is_comments_enabled=True,
    )
    path = f"/api/public/anchor/{board.anchor}/issues/{issue.id}/comments/"
    response = session_client.post(path, {"comment_html": "<p>A concrete public result.</p>"}, format="json")
    assert response.status_code == 201, response.data
    event = ActivitySourceEvent.objects.get(source_kind="comment", source_id=response.data["id"])
    assert event.time_quality == "native" and event.authenticated_writer_id == create_user.id
    foreign = Project.objects.create(workspace=issue.workspace, name="Other", identifier="OTHER")
    foreign_issue = Issue.objects.create(workspace=issue.workspace, project=foreign, name="Private")
    foreign_path = f"/api/public/anchor/{board.anchor}/issues/{foreign_issue.id}/comments/"
    rejected = session_client.post(foreign_path, {"comment_html": "<p>Wrong scope</p>"}, format="json")
    assert rejected.status_code == 404, rejected.data
    assert not IssueComment.objects.filter(issue=foreign_issue).exists()


@pytest.mark.parametrize("migration", [False, "true"])
def test_intake_description_writer_captures_native_edit_and_migration(session_client, issue, migration):
    intake = Intake.objects.create(project=issue.project, workspace=issue.workspace, name="Intake")
    IntakeIssue.objects.create(project=issue.project, workspace=issue.workspace, intake=intake, issue=issue)
    path = f"/api/workspaces/{issue.workspace.slug}/projects/{issue.project_id}/intake-issues/{issue.id}/"
    response = session_client.patch(
        path,
        {
            "issue": {"description_html": "<p>Intake investigation result</p>"},
            "skip_activity": migration,
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    event = ActivitySourceEvent.objects.get(issue=issue, is_baseline=False)
    assert event.time_quality == ("import" if migration else "native")
    assert (event.occurred_at is None) == bool(migration)


def test_projections_wait_for_source_commit_and_do_not_escape_rollback():
    task = Mock()
    with transaction.atomic():
        task_after_commit(task, source="committed")
        task.delay.assert_not_called()
    task.delay.assert_called_once_with(source="committed")
    task.reset_mock()
    with pytest.raises(RuntimeError), transaction.atomic():
        task_after_commit(task, source="rolled-back")
        raise RuntimeError("rollback")
    task.delay.assert_not_called()
