# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Real database, API auth, assignability and competing chat identity writes."""

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from django.core.cache import cache
from django.db import connections
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework.test import APIClient

from plane.api.views.chat_agent import chat_agent_key, ensure_identity
from plane.db.models import (
    APIToken,
    Issue,
    Module,
    Page,
    Project,
    ProjectAgentRegistration,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.middleware.logger import APITokenLogMiddleware

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    settings.APP_BASE_URL = settings.WEB_URL = "http://plane-app.test"
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Existing chat test", identifier="CHAT")
    ProjectMember.objects.create(project=project, member=create_user, role=20)
    State.objects.create(project=project, workspace=workspace, name="Backlog", group="backlog", default=True)
    return project


@pytest.fixture
def payload():
    return {
        "display_name": "Codex chat acceptance",
        "role": "Implement firmware builds",
        "host_id": "local",
        "setup": {"bundle_revision": "a" * 40, "skills": [], "mcp_servers": ["plane_lan"], "verified_at": None},
    }


def url(workspace, project, thread_id):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/chat-agents/{thread_id}/"


def put(client, route, payload, revision=0):
    return client.put(route, payload, format="json", HTTP_IF_MATCH=f'"{revision}"')


def counts():
    return tuple(
        model.objects.count() for model in (Project, User, APIToken, Module, Page, ProjectMember, WorkspaceMember)
    )


def test_two_chats_share_assignable_identity_repeat_rename_and_public_read(api_key_client, workspace, project, payload):
    thread_id, other_thread = uuid4(), uuid4()
    route = url(workspace, project, thread_id)
    before = Project.objects.count(), Module.objects.count(), Page.objects.count()
    first = put(api_key_client, route, payload)
    assert first.status_code == 201, first.data
    assert first["ETag"] == '"1"' and first["Cache-Control"] == "no-store"
    data, agent = first.data, first.data["agent"]
    assert agent["agent_key"] == chat_agent_key(thread_id)
    assert agent["binding"]["thread_id"] == str(thread_id)
    assert agent["role"] == payload["role"]
    assert agent["identity_key"] == data["identity"]["agent_key"] == "project"
    assert agent["user_id"] == data["identity"]["user_id"]
    user = User.objects.get(id=agent["user_id"])
    assert user.is_active and user.is_managed and not user.is_bot and not user.has_usable_password()
    assert ProjectMember.objects.get(project=project, member=user).role == 15
    assert WorkspaceMember.objects.get(workspace=workspace, member=user).role == 15
    assert (Project.objects.count(), Module.objects.count(), Page.objects.count()) == before
    count = counts()
    assert put(api_key_client, route, deepcopy(payload), 1).data == data
    assert counts() == count
    read = api_key_client.get(route)
    assert "credential" not in read.data and read["ETag"] == first["ETag"]
    assert read.data == {key: value for key, value in data.items() if key != "credential"}
    assert api_key_client.get(route + "?include_credential=true").data == data
    second = put(api_key_client, url(workspace, project, other_thread), payload)
    assert second.status_code == 201
    assert second.data["agent"]["user_id"] == agent["user_id"]
    assert second.data["agent"]["id"] != agent["id"]
    assert second.data["identity"] == data["identity"]
    assert second.data["credential"] == data["credential"]
    assert counts() == count
    assert second.data["agent"]["binding"]["thread_id"] == str(other_thread)
    payload["display_name"], payload["role"] = "Renamed chat", "Review firmware"
    renamed = put(api_key_client, route, payload, 1)
    assert renamed.status_code == 200 and renamed.data["agent"]["revision"] == 2
    assert renamed.data["agent"]["user_id"] == agent["user_id"]
    assert renamed.data["credential"] == data["credential"]
    assert renamed.data["membership"]["id"] == data["membership"]["id"]
    assert renamed.data["agent"]["binding"] == agent["binding"]
    user.refresh_from_db()
    assert user.display_name == "Codex project"
    assert (
        api_key_client.get(url(workspace, project, other_thread)).data["agent"]["role"] == "Implement firmware builds"
    )
    # Picker roster + actual issue persistence are separate checks: invalid
    # assignees are otherwise silently dropped by Plane's issue serializer.
    roster = api_key_client.get(f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/members/")
    assert roster.status_code == 200 and str(user.id) in json.dumps(roster.data, default=str)
    issue_url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/issues/"
    with patch("plane.api.views.issue.issue_activity") as activity:
        issue = api_key_client.post(
            issue_url, {"name": "Safe chat assignment test", "assignees": [str(user.id)]}, format="json"
        )
    assert issue.status_code == 201, issue.data
    assert activity.delay.call_args.kwargs["notification"] is True
    read_issue = api_key_client.get(issue_url + str(issue.data["id"]) + "/")
    assert read_issue.data["assignees"] == [str(user.id)]
    assert Issue.objects.get(id=issue.data["id"]).assignees.filter(id=user.id).exists()


def test_stale_writes_immutable_binding_and_legacy_slot_is_preserved(api_key_client, workspace, project, payload):
    legacy = ProjectAgentRegistration.objects.create(
        project=project,
        runtime="codex",
        agent_key="project",
        display_name="Persistent actor",
        binding={"thread_id": str(uuid4())},
    )
    legacy = ensure_identity(project, "project")
    ProjectMember.objects.filter(project=project, member=legacy.user).update(role=20)
    WorkspaceMember.objects.filter(workspace=workspace, member=legacy.user).update(role=20)
    legacy_before = deepcopy(legacy.binding), legacy.user_id, legacy.api_token_id, legacy.revision
    thread_id = uuid4()
    route = url(workspace, project, thread_id)
    assert api_key_client.put(route, payload, format="json").status_code == 428
    created = put(api_key_client, route, payload)
    assert created.status_code == 201
    changed = {**payload, "role": "Stale role"}
    assert put(api_key_client, route, changed, 0).status_code == 412
    assert api_key_client.get(route).data["agent"]["role"] == payload["role"]
    assert put(api_key_client, route, {**payload, "thread_id": str(uuid4())}, 1).status_code == 400
    key = chat_agent_key(thread_id)
    registry_url = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/agent-registrations/codex/{key}/"
    assert api_key_client.get(registry_url).data["user_id"] == created.data["agent"]["user_id"]
    assert put(api_key_client, registry_url, {}, 1).status_code == 400
    legacy.refresh_from_db()
    assert legacy.display_name == "Persistent actor"
    assert (legacy.binding, legacy.user_id, legacy.api_token_id, legacy.revision) == legacy_before
    assert ProjectMember.objects.get(project=project, member=legacy.user).role == 20
    assert WorkspaceMember.objects.get(workspace=workspace, member=legacy.user).role == 20


def test_admin_credentials_public_member_read_and_missing_project(
    api_key_client, workspace, project, create_user, payload
):
    route = url(workspace, project, uuid4())
    created = put(api_key_client, route, payload)
    assert created.status_code == 201
    own = APIClient()
    own.credentials(HTTP_X_API_KEY=created.data["credential"]["api_key"])
    assert own.get(route).status_code == 200
    assert own.get(route + "?include_credential=true").status_code == 403
    assert put(own, route, payload, 1).status_code == 403
    missing = f"/api/v1/workspaces/{workspace.slug}/projects/{uuid4()}/chat-agents/{uuid4()}/"
    assert api_key_client.get(missing).status_code in {403, 404}
    before = counts()
    assert put(api_key_client, missing, payload).status_code in {403, 404}
    assert counts() == before
    member = ProjectMember.objects.get(project=project, member=create_user)
    member.role = 5
    member.save()
    assert api_key_client.get(route).status_code == 200
    assert api_key_client.get(route + "?include_credential=true").status_code == 403
    member.is_active = False
    member.save()
    assert api_key_client.get(route).status_code == 403


def test_scoped_key_cannot_join_other_workspace(api_key_client, api_token, workspace, create_user, payload):
    other = Workspace.objects.create(name="Other chat scope", slug="chat-other", owner=create_user)
    WorkspaceMember.objects.create(workspace=other, member=create_user, role=20)
    project = Project.objects.create(workspace=other, name="Other", identifier="OTHER")
    ProjectMember.objects.create(project=project, member=create_user, role=20)
    api_token.workspace = workspace
    api_token.save()
    assert put(api_key_client, url(other, project, uuid4()), payload).status_code == 403
    assert not ProjectAgentRegistration.objects.exists()


def test_explicit_identity_is_shared_and_chat_cannot_switch_identity(api_key_client, workspace, project, payload):
    payload["identity_key"] = "build-engineer"
    first_route = url(workspace, project, uuid4())
    first = put(api_key_client, first_route, payload)
    second = put(api_key_client, url(workspace, project, uuid4()), payload)
    assert first.status_code == second.status_code == 201
    assert first.data["identity"]["agent_key"] == "build-engineer"
    assert first.data["identity"]["user_id"] == second.data["identity"]["user_id"]
    assert first.data["identity"]["setup"] == payload["setup"]
    updated_setup = {**payload["setup"], "bundle_revision": "c" * 40}
    renamed = put(api_key_client, first_route, {**payload, "setup": updated_setup}, 1)
    assert renamed.status_code == 200
    assert renamed.data["agent"]["setup"] == updated_setup
    assert renamed.data["identity"]["setup"] == first.data["identity"]["setup"]
    assert renamed.data["identity"]["revision"] == first.data["identity"]["revision"]
    before = counts()
    switched = put(api_key_client, first_route, {**payload, "identity_key": "other-identity"}, 2)
    assert switched.status_code == 409 and counts() == before
    assert api_key_client.get(first_route).data["identity"] == first.data["identity"]
    # A later architecture registration may coexist with new identity/chat
    # metadata, without mistaking those rows for a conflicting repository.
    from plane.api.views.project_provisioning import project_for_key

    project.external_source, project.external_id = "agent-platform", "example-repository"
    project.save()
    assert (
        project_for_key(workspace, "example-repository", {"repository_url": "https://github.com/example/repository"})
        == project
    )


@pytest.mark.parametrize("key", ["chat-" + "a" * 32, "UPPER", "../other", "", "x" * 65])
def test_invalid_identity_keys_cannot_allocate_accounts(api_key_client, workspace, project, payload, key):
    before = counts()
    assert put(api_key_client, url(workspace, project, uuid4()), {**payload, "identity_key": key}).status_code == 400
    assert counts() == before


def test_fresh_project_identity_supports_legacy_registry_binding(api_key_client, workspace, project, payload):
    joined = put(api_key_client, url(workspace, project, uuid4()), payload)
    assert joined.status_code == 201
    registry = f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/agent-registrations/codex/project/"
    read = api_key_client.get(registry)
    assert read.status_code == 200
    assert set(read.data["setup"]) == {"bundle_revision", "skills", "mcp_servers", "verified_at"}
    assert read.data["setup"] == payload["setup"]
    desired = {key: read.data[key] for key in ("display_name", "setup", "active")}
    desired["binding"] = {"binding_id": str(uuid4()), "thread_id": str(uuid4()), "host_id": "local"}
    rebound = put(api_key_client, registry, desired, read.data["revision"])
    assert rebound.status_code == 200
    assert rebound.data["user_id"] == joined.data["identity"]["user_id"]
    assert rebound.data["setup"] == payload["setup"]
    assert str(ProjectAgentRegistration.objects.get(id=rebound.data["id"]).api_token.user_id) == rebound.data["user_id"]


@pytest.mark.parametrize(
    "collision", ["reserved_user", "foreign_slot", "wrong_binding", "inactive_user", "inactive_token"]
)
def test_collisions_are_observable_without_adopting_or_retargeting(
    api_key_client, workspace, project, payload, collision
):
    thread_id = uuid4()
    key, route = chat_agent_key(thread_id), url(workspace, project, thread_id)
    revision = 0
    if collision == "reserved_user":
        username = f"agent_codex_{uuid5(NAMESPACE_URL, f'plane:{project.id}:codex:project').hex}"
        User.objects.create(username=username, email=f"{username}@agents.plane.invalid")
    elif collision == "foreign_slot":
        ProjectAgentRegistration.objects.create(
            project=project, runtime="codex", agent_key=key, display_name="Foreign slot"
        )
        revision = 1
    else:
        created = put(api_key_client, route, payload)
        registration = ProjectAgentRegistration.objects.get(id=created.data["agent"]["id"])
        revision = 1
        if collision == "wrong_binding":
            registration.binding["thread_id"] = str(uuid4())
            registration.save()
        elif collision == "inactive_user":
            registration.user.is_active = False
            registration.user.save()
        else:
            registration.api_token.is_active = False
            registration.api_token.save()
    before = counts()
    assert put(api_key_client, route, payload, revision).status_code == 409
    assert counts() == before


def test_reconcile_membership_and_missing_token_preserves_identity(api_key_client, workspace, project, payload):
    route = url(workspace, project, uuid4())
    created = put(api_key_client, route, payload)
    registration = ProjectAgentRegistration.objects.get(id=created.data["agent"]["id"])
    ProjectMember.objects.filter(project=project, member=registration.user).update(is_active=False, role=5)
    registration.api_token.delete(soft=False)
    repaired = put(api_key_client, route, payload, 1)
    assert repaired.status_code == 200 and repaired.data["agent"]["revision"] == 2
    assert repaired.data["agent"]["user_id"] == created.data["agent"]["user_id"]
    assert repaired.data["membership"]["is_active"] and repaired.data["membership"]["role"] == 15
    assert Project.objects.count() == 1 and User.objects.count() == 2


@pytest.mark.parametrize("method", ["get", "put"])
def test_chat_credentials_never_enter_api_logs(monkeypatch, method):
    log = Mock()
    monkeypatch.setattr("plane.middleware.logger.process_logs.delay", log)
    request = RequestFactory().generic(
        method.upper(),
        "/api/v1/workspaces/example/projects/x/chat-agents/x/",
        data=b'{"api_key":"request-secret"}',
        content_type="application/json",
        HTTP_X_API_KEY="auth-secret",
    )
    response = HttpResponse('{"credential":{"api_key":"response-secret"}}', content_type="application/json")
    APITokenLogMiddleware(lambda request: response).process_request(request, response, request.body)
    stored = log.call_args.kwargs["log_data"]
    assert stored["body"] == stored["response_body"] == "[REDACTED chat agent provisioning]"
    assert all(secret not in json.dumps(stored) for secret in ("auth-secret", "request-secret", "response-secret"))


@pytest.mark.django_db(transaction=True)
def test_concurrent_chat_creation_has_one_identity_and_cas_winner(api_token, workspace, project, payload):
    route = url(workspace, project, uuid4())
    barrier = Barrier(2)

    def write(index):
        connections.close_all()
        try:
            client = APIClient()
            client.credentials(HTTP_X_API_KEY=api_token.token)
            barrier.wait(timeout=10)
            response = put(client, route, {**payload, "display_name": f"Competing {index}"})
            return response.status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, range(2)))
    assert sorted(results) == [201, 412]
    assert ProjectAgentRegistration.objects.count() == 2 and User.objects.count() == 2
    assert APIToken.objects.count() == 2
