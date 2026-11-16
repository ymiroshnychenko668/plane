"""Assessment API contracts with real rows, locks, source visibility and evaluator."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from uuid import uuid4

import pytest
from django.db import connections
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import (
    ActivityAssessment,
    ActivityAssessmentReceipt,
    ActivityAssessmentRevision,
    ActivityCoverage,
    ActivitySourceEvent,
)
from plane.tests.contract.api.test_project_views import actor, issue
from plane.tests.contract.api.test_project_views import local_dependencies as local_dependencies
from plane.tests.contract.api.test_project_views import project as project

pytestmark = [pytest.mark.contract, pytest.mark.django_db]
POLICY = "meaningful_activity_v1"


def path(workspace, project, item=None, suffix=""):
    base = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/"
    if item:
        return base + f"work-items/{item.id}/activity-assessments/{suffix}"
    return base + "activity-assessments/pending/"


def source(project, item, ago=1, **overrides):
    moment = timezone.now() - timedelta(days=ago)
    return ActivitySourceEvent.objects.create(
        **{
            "issue": item,
            "project": project,
            "source_kind": "description",
            "source_id": item.id,
            "source_revision": 1,
            "capture_transaction": uuid4().int % 2**60,
            "source_content": {"name": item.name, "description_html": "<p>Investigated a reproducible failure.</p>"},
            "before_content": {"description_html": "<p>Investigating.</p>"},
            "occurred_at": moment,
            "observed_at": moment,
            "time_quality": "native",
            **overrides,
        }
    )


def payload(event, **overrides):
    return {
        "event_id": str(event.id),
        "source_revision": event.source_revision,
        "policy_version": POLICY,
        "decision": "meaningful",
        "category": "investigation",
        "rationale": "Identified a reproducible failure.",
        "request_id": f"assess-{uuid4()}",
        **overrides,
    }


def test_assess_cas_replay_retract_history_and_native(api_key_client, session_client, workspace, project):
    item = issue(project)
    event = source(project, item)
    data = payload(event)
    url = path(workspace, project, item, "assess/")
    assert api_key_client.post(url, data, format="json").status_code == 428
    first = api_key_client.post(url, data, format="json", HTTP_IF_MATCH='"0"')
    assert first.status_code == 201 and first.data["revision"] == 1, first.data
    assert first["ETag"] == '"1"' and first["Cache-Control"] == "no-store"
    result_id = first.data["id"]
    repeat = api_key_client.post(url, data, format="json", HTTP_IF_MATCH='"0"')
    assert repeat.status_code == 200 and repeat.data == first.data
    conflict = api_key_client.post(url, {**data, "rationale": "Different"}, format="json", HTTP_IF_MATCH='"0"')
    assert conflict.status_code == 409
    stale = api_key_client.post(url, payload(event), format="json", HTTP_IF_MATCH='"0"')
    assert stale.status_code == 412
    candidates = api_key_client.get(path(workspace, project, item, "candidates/"), {"policy_version": POLICY})
    assert candidates.status_code == 200 and candidates.data["results"][0]["assessment"]["id"] == result_id
    native_get = path(workspace, project, item, f"{result_id}/").replace("/api/v1/", "/api/")
    assert session_client.get(native_get).data == first.data
    retraction = {"request_id": "retract-request-001", "reason": "Evidence was misinterpreted."}
    retract_path = native_get + "retract/"
    response = session_client.post(retract_path, retraction, format="json", HTTP_IF_MATCH='W/"1"')
    assert response.status_code == 200 and response.data["revision"] == 2, response.data
    assert response.data["decision"] == "needs_review" and response.data["category"] == "unknown"
    assert session_client.post(retract_path, retraction, format="json", HTTP_IF_MATCH='"1"').status_code == 200
    assert ActivityAssessment.objects.count() == 1
    assert ActivityAssessmentRevision.objects.count() == 2 and ActivityAssessmentReceipt.objects.count() == 2
    assert api_key_client.post(url, data, format="json", HTTP_IF_MATCH='"0"').status_code == 409
    pending = api_key_client.get(path(workspace, project), {"policy_version": POLICY})
    assert {row["event_id"] for row in pending.data["results"]} == {str(event.id)}


def test_policy_bounds_strict_fields_scope_and_source_revision(api_key_client, workspace, project):
    item = issue(project)
    other = issue(project, "Different issue")
    event = source(project, item)
    url = path(workspace, project, item, "assess/")
    assert api_key_client.get(path(workspace, project)).status_code == 400
    assert api_key_client.get(path(workspace, project), {"policy_version": POLICY, "per_page": 101}).status_code == 400
    for overrides in (
        {"source_revision": True},
        {"decision": "meaningful", "category": "routine"},
        {"rationale": ""},
        {"request_id": "tiny"},
        {"policy_version": "unknown"},
        {"actor": "forged"},
    ):
        response = api_key_client.post(url, payload(event, **overrides), format="json", HTTP_IF_MATCH='"0"')
        assert response.status_code == 400, response.data
    assert (
        api_key_client.post(url, payload(event, source_revision=2), format="json", HTTP_IF_MATCH='"0"').status_code
        == 409
    )
    cross_issue = api_key_client.post(
        path(workspace, project, other, "assess/"), payload(event), format="json", HTTP_IF_MATCH='"0"'
    )
    assert cross_issue.status_code == 404
    baseline = source(project, item, is_baseline=True)
    assert api_key_client.post(url, payload(baseline), format="json", HTTP_IF_MATCH='"0"').status_code == 404
    assert not ActivityAssessment.objects.exists()


def test_pending_cursor_and_guest_source_visibility(api_key_client, workspace, project):
    item = issue(project)
    events = [source(project, item, ago=days) for days in (3, 2, 1)]
    first = api_key_client.get(path(workspace, project), {"policy_version": POLICY, "per_page": 1})
    assert first.status_code == 200 and first.data["results"][0]["event_id"] == str(events[0].id)
    cursor = first.data["next_cursor"]
    second = api_key_client.get(path(workspace, project), {"policy_version": POLICY, "cursor": cursor, "per_page": 1})
    assert second.status_code == 200 and second.data["results"][0]["event_id"] == str(events[1].id)
    _, _, another = actor(workspace, project)
    assert another.get(path(workspace, project), {"policy_version": POLICY, "cursor": cursor}).status_code == 400
    assert (
        api_key_client.get(
            path(workspace, project, item, "candidates/"), {"policy_version": POLICY, "cursor": cursor}
        ).status_code
        == 400
    )
    _, _, guest = actor(workspace, project, role=5)
    project.guest_view_all_features = True
    project.save()
    assert guest.get(path(workspace, project), {"policy_version": POLICY}).status_code == 403
    assert guest.get(path(workspace, project, item, "summary/"), {"policy_version": POLICY}).status_code == 403
    assert (
        guest.post(
            path(workspace, project, item, "assess/"), payload(events[0]), format="json", HTTP_IF_MATCH='"0"'
        ).status_code
        == 403
    )


def test_redacted_deleted_and_cross_project_sources_are_not_exposed(api_key_client, workspace, project):
    item = issue(project)
    event = source(project, item)
    assessed = api_key_client.post(
        path(workspace, project, item, "assess/"), payload(event), format="json", HTTP_IF_MATCH='"0"'
    )
    assert assessed.status_code == 201, assessed.data
    ActivitySourceEvent.objects.filter(id=event.id).update(redacted=True)
    assert api_key_client.get(path(workspace, project, item, f"{assessed.data['id']}/")).status_code == 404
    assert (
        api_key_client.get(path(workspace, project, item, "candidates/"), {"policy_version": POLICY}).data["results"]
        == []
    )
    item.delete()
    assert api_key_client.get(path(workspace, project, item, "summary/"), {"policy_version": POLICY}).status_code == 404


def test_summary_and_views_pending_is_independent_of_stale_partition(api_key_client, workspace, project):
    recent = issue(project, "Recent confirmed activity and pending event")
    stale = issue(project, "Fully observed, no activity")
    unknown = issue(project, "Not observed long enough")
    old = timezone.now() - timedelta(days=30)
    for item in (recent, stale):
        ActivityCoverage.objects.update_or_create(
            issue=item, defaults={"captured_from": old, "known_existing_at": old, "native_created_at": old}
        )
    ActivityCoverage.objects.update_or_create(
        issue=unknown, defaults={"captured_from": timezone.now(), "known_existing_at": old, "native_created_at": None}
    )
    event = source(project, recent, ago=2)
    api_key_client.post(path(workspace, project, recent, "assess/"), payload(event), format="json", HTTP_IF_MATCH='"0"')
    unresolved = source(project, recent, ago=1)
    summary = api_key_client.get(path(workspace, project, recent, "summary/"), {"policy_version": POLICY})
    assert summary.status_code == 200 and summary.data["pending_count"] == 1
    assert summary.data["last_meaningful_at"] == event.occurred_at.isoformat().replace("+00:00", "Z")
    pending = api_key_client.get(path(workspace, project), {"policy_version": POLICY})
    assert str(unresolved.id) in {row["event_id"] for row in pending.data["results"]}
    definition = {
        "filter_meta": {"version": 1, "policy_version": POLICY, "source_schema_version": "task_text_v1"},
        "rich_filters": {"and": [{"meaningful_activity__older_than_days": 7}]},
    }
    view_url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/views/preview/"
    matches = api_key_client.post(view_url, definition, format="json")
    assert matches.status_code == 200, matches.data
    assert {str(row["id"]) for row in matches.data["results"]} == {str(stale.id)}
    assert matches.data["counts"] == {"matches": 1, "needs_review": 1}
    review = api_key_client.post(view_url, {**definition, "partition": "needs_review"}, format="json")
    assert {str(row["id"]) for row in review.data["results"]} == {str(unknown.id)}
    assert review.data["assessment_complete"] is False


@pytest.mark.django_db(transaction=True)
def test_concurrent_assessment_cas_allows_one_writer(api_token, workspace, project):
    item = issue(project)
    event = source(project, item)
    url = path(workspace, project, item, "assess/")
    barrier = Barrier(2)

    def worker():
        connections.close_all()
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        barrier.wait(timeout=10)
        try:
            result = client.post(url, payload(event), format="json", HTTP_IF_MATCH='"0"')
            return result.status_code, result.data
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(status for status, _ in results) == [201, 412], results
    assert ActivityAssessment.objects.count() == 1
    assert ActivityAssessmentRevision.objects.count() == 1 and ActivityAssessmentReceipt.objects.count() == 1
