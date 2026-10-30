# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The current API credential, not an assignee argument, selects the task owner."""

from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import APIToken, Issue, IssueAssignee, Project, ProjectMember, State, User, Workspace

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="My tasks", identifier="MYT")
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=15)
    return project


@pytest.fixture
def make_issue(workspace, project):
    def create(name, assignee=None, group="unstarted", **kwargs):
        state, _ = State.objects.get_or_create(
            workspace=workspace,
            project=project,
            group=group,
            defaults={"name": group, "color": "#000000"},
        )
        issue = Issue.objects.create(workspace=workspace, project=project, state=state, name=name, **kwargs)
        if assignee is not None:
            IssueAssignee.objects.create(workspace=workspace, project=project, issue=issue, assignee=assignee)
        return issue

    return create


def url(workspace, project):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/my-work-items/"


def test_current_api_user_receives_only_own_active_project_assignments(
    api_key_client,
    create_user,
    workspace,
    project,
    make_issue,
):
    colleague = User.objects.create(email="colleague@plane.test", username="colleague")
    ProjectMember.objects.create(workspace=workspace, project=project, member=colleague, role=15)
    own = make_issue("Own", create_user)
    done = make_issue("Done", create_user, group="completed")
    other = make_issue("Colleague", colleague)
    make_issue("Unassigned")
    removed = make_issue("Removed assignment", create_user)
    IssueAssignee.objects.filter(issue=removed, assignee=create_user).update(deleted_at=timezone.now())
    make_issue("Archived", create_user, archived_at=timezone.now())
    make_issue("Deleted", create_user, deleted_at=timezone.now())
    make_issue("Draft", create_user, is_draft=True)
    elsewhere = Project.objects.create(workspace=workspace, name="Elsewhere", identifier="ELS")
    foreign = Issue.objects.create(workspace=workspace, project=elsewhere, name="Other project")
    IssueAssignee.objects.create(workspace=workspace, project=elsewhere, issue=foreign, assignee=create_user)

    response = api_key_client.get(url(workspace, project))
    assert response.status_code == 200, response.data
    assert [item["id"] for item in response.json()["results"]] == [str(own.id), str(done.id)]
    assert response.data["total_results"] == 2
    assert response.data["results"][0]["state"]["group"] == "unstarted"

    token = APIToken.objects.create(user=colleague, label="Colleague", token="colleague-task-test-key")
    other_session = APIClient()
    other_session.credentials(HTTP_X_API_KEY=token.token)
    response = other_session.get(url(workspace, project))
    assert response.status_code == 200, response.data
    assert [item["id"] for item in response.json()["results"]] == [str(other.id)]


def test_state_filter_and_cursor_cover_only_current_user_tasks(
    api_key_client,
    create_user,
    workspace,
    project,
    make_issue,
):
    backlog = make_issue("Backlog", create_user, group="backlog")
    todo = make_issue("Todo", create_user)
    make_issue("Started", create_user, group="started")
    make_issue("Completed", create_user, group="completed")
    query = {"state_group": "backlog,unstarted", "per_page": 1, "expand": "state"}
    response = api_key_client.get(url(workspace, project), query)
    assert response.status_code == 200, response.data
    assert [item["id"] for item in response.json()["results"]] == [str(backlog.id)]
    assert response.data["total_results"] == 2
    assert response.data["next_page_results"] is True
    response = api_key_client.get(url(workspace, project), {**query, "cursor": response.data["next_cursor"]})
    assert response.status_code == 200, response.data
    assert [item["id"] for item in response.json()["results"]] == [str(todo.id)]
    assert response.data["next_page_results"] is False
    response = api_key_client.get(url(workspace, project), {"state_group": "cancelled"})
    assert response.status_code == 200, response.data
    assert response.data["results"] == []
    assert response.data["total_results"] == 0
    assert response.data["next_page_results"] is False


@pytest.mark.parametrize(
    "query", [{"state_group": "blocked"}, {"state_group": ""}, {"assignees": "me"}, {"user_id": "someone-else"}]
)
def test_invalid_filters_cannot_change_the_authenticated_owner(api_key_client, workspace, project, query):
    response = api_key_client.get(url(workspace, project), query)
    assert response.status_code == 400, response.data


@pytest.mark.parametrize("denial", ["inactive-membership", "missing-membership", "other-workspace-key"])
def test_current_project_and_workspace_access_is_required(
    api_key_client,
    api_token,
    create_user,
    workspace,
    project,
    make_issue,
    denial,
):
    make_issue("Own task without active membership", create_user)
    membership = ProjectMember.objects.filter(project=project, member=create_user)
    if denial == "inactive-membership":
        membership.update(is_active=False)
    elif denial == "missing-membership":
        membership.delete()
    else:
        api_token.workspace = Workspace.objects.create(name="Other", slug="other", owner=create_user)
        api_token.save(update_fields=["workspace"])
    assert api_key_client.get(url(workspace, project)).status_code == 403


def test_authenticated_api_session_is_required(api_client, create_user, workspace, project):
    assert api_client.get(url(workspace, project)).status_code in (401, 403)
    api_client.force_login(create_user)
    assert api_client.get(url(workspace, project)).status_code in (401, 403)


@pytest.mark.parametrize("guest_view_all_features", [False, True])
def test_role_downgrade_preserves_project_guest_visibility(
    api_key_client, create_user, workspace, project, make_issue, guest_view_all_features
):
    author = User.objects.create(email="author@plane.test", username="author")
    own = make_issue("Created by me", create_user)
    assigned = make_issue("Assigned by colleague", create_user)
    own.save(created_by_id=create_user.id)
    assigned.save(created_by_id=author.id)
    response = api_key_client.get(url(workspace, project))
    assert response.status_code == 200
    assert {item["id"] for item in response.json()["results"]} == {str(own.id), str(assigned.id)}

    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    project.guest_view_all_features = guest_view_all_features
    project.save(update_fields=["guest_view_all_features"])
    response = api_key_client.get(url(workspace, project))
    assert response.status_code == 200
    expected = {str(own.id), str(assigned.id)} if guest_view_all_features else {str(own.id)}
    assert {item["id"] for item in response.json()["results"]} == expected
