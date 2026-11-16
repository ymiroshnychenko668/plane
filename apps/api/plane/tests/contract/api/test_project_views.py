"""Project Views against disposable PostgreSQL and both authentication surfaces."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import connections
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import (
    APIToken,
    Issue,
    IssueAssignee,
    IssueView,
    IssueViewCreateReceipt,
    Project,
    ProjectMember,
    State,
    User,
    UserFavorite,
    WorkspaceMember,
)

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    result = Project.objects.create(workspace=workspace, name="Views", identifier="VIEW")
    ProjectMember.objects.create(workspace=workspace, project=result, member=create_user, role=20)
    return result


@pytest.fixture
def definition():
    return {
        "name": "Unassigned",
        "filter_meta": {"version": 1},
        "request_id": "views-create-request-001",
        "rich_filters": {
            "and": [
                {"state_group__in": "backlog,unstarted,started"},
                {"is_archived__exact": False},
                {"is_draft__exact": False},
                {"assignee_id__is_empty": True},
            ]
        },
    }


def url(workspace, project, suffix=""):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/views/{suffix}"


def create(client, workspace, project, definition, **overrides):
    response = client.post(url(workspace, project), {**deepcopy(definition), **overrides}, format="json")
    assert response.status_code == 201, response.data
    return response.data


def actor(workspace, project, role=15):
    suffix = uuid4().hex
    user = User.objects.create(email=f"views-{suffix}@example.invalid", username=f"view-{suffix}")
    WorkspaceMember.objects.create(workspace=workspace, member=user, role=role)
    membership = ProjectMember.objects.create(workspace=workspace, project=project, member=user, role=role)
    token = APIToken.objects.create(user=user, workspace=workspace, token=f"views-test-{suffix}")
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=token.token)
    return user, membership, client


def issue(project, name="Issue", **kwargs):
    state = State.objects.filter(project=project, group="started").first()
    if state is None:
        state = State.objects.create(workspace=project.workspace, project=project, name="Active", group="started")
    return Issue.objects.create(workspace=project.workspace, project=project, state=state, name=name, **kwargs)


def test_crud_native_readback_and_idempotency(api_key_client, session_client, workspace, project, definition):
    data = create(api_key_client, workspace, project, definition)
    assert data["revision"] == 1 and data["can_edit"] and data["can_delete"]
    path = url(workspace, project, f"{data['id']}/")
    assert api_key_client.post(path, definition, format="json").status_code == 405
    assert api_key_client.patch(url(workspace, project), {}, format="json").status_code == 405
    response = api_key_client.get(path)
    assert response["ETag"] == '"1"' and response["Cache-Control"] == "no-store"
    native_path = path.replace("/api/v1/", "/api/")
    UserFavorite.objects.create(
        project=project,
        workspace=workspace,
        user_id=data["owned_by"],
        entity_type="view",
        entity_identifier=data["id"],
    )
    native = session_client.get(native_path)
    assert native.data["filter_meta"] == {"version": 1} and native.data["is_favorite"] is True
    retry = api_key_client.post(url(workspace, project), definition, format="json")
    assert retry.status_code == 200 and retry.data["id"] == data["id"]
    conflict = api_key_client.post(url(workspace, project), {**definition, "name": "Other"}, format="json")
    assert conflict.status_code == 409
    assert api_key_client.patch(path, {"name": "Renamed"}, format="json").status_code == 428
    response = session_client.patch(native_path, {"name": "Renamed"}, format="json", HTTP_IF_MATCH='W/"1"')
    assert response.status_code == 200 and response.data["revision"] == 2, response.data
    stale = api_key_client.patch(path, {"name": "Stale"}, format="json", HTTP_IF_MATCH='"1"')
    assert stale.status_code == 412
    assert session_client.put(native_path, {"name": "Bypass"}, format="json").status_code == 428
    assert api_key_client.delete(path).status_code == 428
    assert session_client.delete(native_path, HTTP_IF_MATCH='"2"').status_code == 204
    assert api_key_client.post(url(workspace, project), definition, format="json").status_code == 404
    assert IssueViewCreateReceipt.objects.count() == 1


def test_names_are_not_upsert_keys(api_key_client, workspace, project, definition):
    first = create(api_key_client, workspace, project, definition)
    second = create(api_key_client, workspace, project, definition, request_id="different-request-002")
    assert first["id"] != second["id"]
    page = api_key_client.get(url(workspace, project), {"per_page": 1, "name": "Unassigned"})
    assert page.status_code == 200 and page.data["next_page_results"]
    next_page = api_key_client.get(
        url(workspace, project), {"per_page": 1, "name": "Unassigned", "cursor": page.data["next_cursor"]}
    )
    assert next_page.status_code == 200 and not next_page.data["next_page_results"]
    assert next_page.data["results"][0]["id"] != page.data["results"][0]["id"]


def test_unassigned_relations_filters_preview_and_read_only(
    api_key_client, session_client, create_user, workspace, project, definition
):
    active = issue(project, "No assignee")
    removed = issue(project, "Removed assignment")
    relation = IssueAssignee.objects.create(project=project, workspace=workspace, issue=removed, assignee=create_user)
    relation.delete()
    assigned = issue(project, "Assigned")
    user, _, _ = actor(workspace, project)
    user.is_active = False
    user.save()
    IssueAssignee.objects.create(project=project, workspace=workspace, issue=assigned, assignee=user)
    issue(project, "Draft", is_draft=True)
    issue(project, "Archived", archived_at=timezone.now())
    data = create(api_key_client, workspace, project, definition)
    path = url(workspace, project, f"{data['id']}/work-items/")
    response = api_key_client.get(path)
    assert response.status_code == 200, response.data
    assert {str(row["id"]) for row in response.data["results"]} == {str(active.id), str(removed.id)}
    assert response.data["counts"] == {"matches": 2, "needs_review": 0}
    assert response.data["policy_version"] is None and response.data["cutoff_at"] is None
    assert response.data["assessment_complete"] is True
    native = session_client.get(path.replace("/api/v1/", "/api/"))
    assert native.data["counts"] == response.data["counts"]
    before = IssueView.objects.count()
    preview = api_key_client.post(
        url(workspace, project, "preview/"),
        {
            "rich_filters": definition["rich_filters"],
            "filter_meta": definition["filter_meta"],
        },
        format="json",
    )
    assert preview.status_code == 200 and preview.data["counts"] == response.data["counts"], preview.data
    assert IssueView.objects.count() == before
    assert api_key_client.get(path, {"partition": "needs_review"}).data["results"] == []


def test_cursor_scope_revision_partition_and_time(api_key_client, workspace, project, definition):
    for name in ("A", "B", "C"):
        issue(project, name)
    data = create(api_key_client, workspace, project, definition)
    path = url(workspace, project, f"{data['id']}/work-items/")
    page = api_key_client.get(path, {"per_page": 1}).data
    cursor = page["next_cursor"]
    second = api_key_client.get(path, {"per_page": 1, "cursor": cursor})
    assert second.status_code == 200 and second.data["evaluated_at"] == page["evaluated_at"]
    assert second.data["results"][0]["id"] != page["results"][0]["id"]
    assert api_key_client.get(path, {"partition": "needs_review", "cursor": cursor}).status_code == 400
    _, _, other = actor(workspace, project)
    assert other.get(path, {"cursor": cursor}).status_code == 400
    assert api_key_client.get(path, {"cursor": cursor + "x"}).status_code == 400
    api_key_client.patch(
        url(workspace, project, f"{data['id']}/"), {"name": "Changed"}, format="json", HTTP_IF_MATCH='"1"'
    )
    assert api_key_client.get(path, {"cursor": cursor}).status_code == 400


def test_private_owner_locked_guest_and_workspace_scope(api_key_client, session_client, workspace, project, definition):
    private = create(api_key_client, workspace, project, definition, access=0)
    public = create(api_key_client, workspace, project, definition, request_id="public-request-002")
    private_path = url(workspace, project, f"{private['id']}/")
    public_path = url(workspace, project, f"{public['id']}/")
    _, member, other = actor(workspace, project)
    assert other.get(private_path).status_code == 404
    assert other.get(public_path).status_code == 200
    assert other.patch(public_path, {"name": "Stolen"}, format="json", HTTP_IF_MATCH='"1"').status_code == 403
    assert other.delete(public_path, HTTP_IF_MATCH='"1"').status_code == 403
    IssueView.objects.filter(id=public["id"]).update(is_locked=True)
    assert api_key_client.patch(public_path, {"name": "Locked"}, format="json", HTTP_IF_MATCH='"1"').status_code == 409
    member.is_active = False
    member.save()
    assert other.get(public_path).status_code == 404
    _, _, guest = actor(workspace, project, role=5)
    project.guest_view_all_features = True
    project.save()
    assert guest.get(public_path).status_code == 200
    assert guest.post(url(workspace, project), definition, format="json").status_code == 403
    semantic = {
        "filter_meta": {
            "version": 1,
            "policy_version": "meaningful_activity_v1",
            "source_schema_version": "task_text_v1",
        },
        "rich_filters": {"and": [{"meaningful_activity__older_than_days": 7}]},
    }
    assert guest.post(url(workspace, project, "preview/"), semantic, format="json").status_code == 403
    # Workspace CRUD may not mutate a project View by guessing its id.
    bypass = f"/api/workspaces/{workspace.slug}/views/{public['id']}/"
    assert session_client.delete(bypass).status_code == 404
    assert IssueView.objects.filter(id=public["id"]).exists()


@pytest.mark.parametrize(
    "patch",
    [
        {"owned_by": "fake"},
        {"query": {}},
        {"filter_meta": {"version": True}},
        {"rich_filters": {"or": [{"assignee_id__is_empty": True}]}},
        {"rich_filters": {"and": [{"assignee_id__is_empty": "true"}]}},
        {"rich_filters": {"and": [{"state_group__in": ["started"]}]}},
        {"rich_filters": {"and": [{"is_archived__exact": "false"}]}},
        {"rich_filters": {"and": [{"meaningful_activity__older_than_days": True}]}},
        {"rich_filters": {"and": [{"meaningful_activity__older_than_days": 0}]}},
        {"display_filters": {"layout": "board"}},
        {"access": True},
        {"request_id": "short"},
    ],
)
def test_strict_definition_rejection(api_key_client, workspace, project, definition, patch):
    response = api_key_client.post(url(workspace, project), {**definition, **patch}, format="json")
    assert response.status_code == 400, response.data
    assert not IssueView.objects.exists()


def test_generic_native_predicates_never_silently_match(session_client, workspace, project):
    from plane.utils.filters.filter_backend import ComplexFilterBackend
    from plane.utils.filters.filterset import IssueFilterSet
    from rest_framework.exceptions import ValidationError
    from plane.utils.issue_filters import issue_filters
    from types import SimpleNamespace

    filters = {"and": [{"assignee_id__is_empty": True}]}
    with pytest.raises(ValidationError):
        ComplexFilterBackend()._apply_json_filter(
            Issue.objects.none(), filters, SimpleNamespace(filterset_class=IssueFilterSet)
        )
    with pytest.raises(ValidationError):
        issue_filters({"meaningful_activity__older_than_days": 7}, "GET")
    response = session_client.post(
        f"/api/workspaces/{workspace.slug}/views/",
        {
            "name": "Invalid global",
            "rich_filters": filters,
        },
        format="json",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_concurrent_same_request_creates_one_view(api_token, workspace, project, definition):
    barrier = Barrier(2)
    path = url(workspace, project)

    def worker():
        connections.close_all()
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        barrier.wait(timeout=10)
        try:
            result = client.post(path, definition, format="json")
            return result.status_code, result.data
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert sorted(status for status, _ in results) == [200, 201], results
    assert results[0][1]["id"] == results[1][1]["id"]
    assert IssueView.objects.count() == 1 and IssueViewCreateReceipt.objects.count() == 1
