# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Real PostgreSQL provisioning and authentication; only document codec/tasks are mocked."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event, local
from time import monotonic, sleep
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import connections
from django.http import HttpResponse
from django.test import RequestFactory
from rest_framework.response import Response
from rest_framework.test import APIClient

from plane.api.views.project_provisioning import ProvisioningInput, manifest_digest
from plane.db.models import (
    APIToken,
    DEFAULT_STATES,
    Module,
    Page,
    Project,
    ProjectAgentRegistration,
    ProjectMember,
    ProjectPage,
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

    def convert(path, data, mutation):
        assert path == "convert-document/" and mutation is False
        return Response(
            {
                "description_html": data["description_html"],
                "description_json": {"type": "doc", "content": []},
                "description_binary": base64.b64encode(b"\x01\x02\x03\x04").decode(),
            }
        )

    codec = Mock(side_effect=convert)
    monkeypatch.setattr("plane.api.views.project_provisioning.call_live", codec)
    return codec


@pytest.fixture
def payload():
    return {
        "schema_version": 1,
        "project": {
            "name": "Example Firmware",
            "identifier": "EXAMPLE",
            "description": "A real project",
            "repository_url": "https://github.com/example/firmware",
        },
        "agent": {"runtime": "codex", "key": "project", "display_name": "Codex Example"},
        "modules": [{"key": "builds", "name": "Builds", "description": "Long builds"}],
        "pages": [{"key": "status", "name": "Status", "description_html": "<p>Initial status</p>"}],
    }


def url(workspace, key="example-firmware"):
    return f"/api/v1/workspaces/{workspace.slug}/project-provisioning/{key}/"


def provision(client, workspace, payload, key="example-firmware"):
    response = client.put(url(workspace, key), payload, format="json")
    assert response.status_code in {200, 201}, response.data
    return response


def counts():
    return tuple(
        model.objects.count()
        for model in (Project, User, APIToken, Module, Page, ProjectAgentRegistration, WorkspaceMember, ProjectMember)
    )


def test_create_repeat_readback_visible_user_and_actor(
    api_key_client, session_client, workspace, payload, create_user, local_dependencies
):
    assert api_key_client.get(url(workspace)).status_code == 404
    response = provision(api_key_client, workspace, payload)
    assert response.status_code == 201 and response["Cache-Control"] == "no-store"
    data = response.data
    assert set(data) == {
        "schema_version",
        "project_key",
        "project",
        "agent",
        "modules",
        "pages",
        "credential",
        "manifest_digest",
    }
    project = Project.objects.get(id=data["project"]["id"])
    agent = User.objects.get(id=data["agent"]["user_id"])
    assert project.external_source == "agent-platform" and project.external_id == "example-firmware"
    assert project.module_view and project.page_view
    assert State.all_state_objects.filter(project=project).count() == len(DEFAULT_STATES)
    assert agent.is_managed and agent.is_active and not agent.is_bot and not agent.has_usable_password()
    assert WorkspaceMember.objects.get(workspace=workspace, member=agent).role == 20
    assert ProjectMember.objects.get(project=project, member=agent).role == 20
    assert ProjectMember.objects.get(project=project, member=create_user).role == 20
    assert Page.objects.get(id=data["pages"][0]["id"]).owned_by == agent
    assert Module.objects.get(id=data["modules"][0]["id"]).lead == agent
    before = counts()
    repeated = provision(api_key_client, workspace, deepcopy(payload))
    assert repeated.data == data and counts() == before
    # Existing templates do not require live service again.
    assert local_dependencies.call_count == 1
    assert api_key_client.get(url(workspace)).data == data
    assert api_key_client.get(url(workspace))["Cache-Control"] == "no-store"

    roster = session_client.get(f"/api/workspaces/{workspace.slug}/projects/{project.id}/members/")
    assert roster.status_code == 200, roster.data
    assert str(agent.id) in json.dumps(roster.data, default=str)
    own_client = APIClient()
    own_client.credentials(HTTP_X_API_KEY=data["credential"]["api_key"])
    created = own_client.post(
        f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/modules/",
        {"name": "Agent authored"},
        format="json",
    )
    assert created.status_code == 201, created.data
    assert Module.objects.get(id=created.data["id"]).created_by_id == agent.id


def test_digest_defaults_order_setup_and_adoption_id_are_deterministic(payload):
    payload["project"].pop("description")
    payload["modules"][0].pop("description")
    serializer = ProvisioningInput(data=payload)
    assert serializer.is_valid(), serializer.errors
    baseline = manifest_digest(serializer.validated_data)
    payload["project"]["description"] = ""
    payload["modules"][0]["description"] = ""
    payload["project"]["existing_project_id"] = "12345678-1234-1234-1234-123456789012"
    payload["setup"] = {"bundle_revision": "a" * 40, "skills": [], "mcp_servers": [], "verified_at": None}
    serializer = ProvisioningInput(data=payload)
    assert serializer.is_valid(), serializer.errors
    assert manifest_digest(serializer.validated_data) == baseline


def test_manual_edits_preserved_and_missing_templates_added(api_key_client, workspace, payload):
    first = provision(api_key_client, workspace, payload).data
    page = Page.objects.get(id=first["pages"][0]["id"])
    page.name, page.description_html = "Human status", "<p>Human content</p>"
    page.save()
    module = Module.objects.get(id=first["modules"][0]["id"])
    module.name, module.description = "Human builds", "Human description"
    module.save()
    payload["pages"][0]["description_html"] = "<p>Updated template</p>"
    payload["pages"].append({"key": "coverage", "name": "Coverage", "description_html": "<p>No measurements yet</p>"})
    second = provision(api_key_client, workspace, payload).data
    page.refresh_from_db()
    module.refresh_from_db()
    assert page.description_html == "<p>Human content</p>" and page.name == "Human status"
    assert module.description == "Human description" and module.name == "Human builds"
    assert len(second["pages"]) == 2 and second["agent"] == first["agent"]
    assert second["manifest_digest"] != first["manifest_digest"]


def test_codec_failure_leaves_no_partial_project_user_or_credential(
    api_key_client, workspace, payload, local_dependencies
):
    before = counts()
    local_dependencies.side_effect = None
    local_dependencies.return_value = Response({"error": "codec down"}, status=503)
    response = api_key_client.put(url(workspace), payload, format="json")
    assert response.status_code == 502
    assert counts() == before


def test_name_collision_requires_explicit_adoption_and_preserves_legacy_binding(
    api_key_client, workspace, create_user, payload
):
    project = Project.objects.create(workspace=workspace, name=payload["project"]["name"], identifier="EXAMPLE")
    legacy = ProjectAgentRegistration.objects.create(
        project=project,
        runtime="codex",
        agent_key="project",
        display_name="Legacy",
        binding={"thread_id": "old-chat"},
        setup={"legacy": True},
    )
    before = counts()
    response = api_key_client.put(url(workspace), payload, format="json")
    assert response.status_code == 409 and counts() == before
    payload["project"]["existing_project_id"] = str(project.id)
    data = provision(api_key_client, workspace, payload).data
    assert data["project"]["id"] == str(project.id)
    legacy.refresh_from_db()
    assert legacy.binding == {"thread_id": "old-chat"} and legacy.setup == {"legacy": True}
    assert str(legacy.id) == data["agent"]["registration_id"] and legacy.user_id is not None


@pytest.mark.parametrize("conflict", ["repository", "adoption_id", "other_external_owner"])
def test_foreign_ownership_conflicts_do_not_mutate(api_key_client, workspace, payload, conflict):
    first = provision(api_key_client, workspace, payload).data
    if conflict == "repository":
        payload["project"]["repository_url"] = "https://github.com/other/repository"
    elif conflict == "adoption_id":
        other = Project.objects.create(workspace=workspace, name="Other", identifier="OTHER")
        payload["project"]["existing_project_id"] = str(other.id)
    else:
        Project.objects.filter(id=first["project"]["id"]).update(external_source="other-owner")
        payload["project"]["existing_project_id"] = first["project"]["id"]
    before = counts()
    response = api_key_client.put(url(workspace), payload, format="json")
    assert response.status_code == 409 and counts() == before


@pytest.mark.parametrize("kind", ["module", "page"])
def test_foreign_asset_collision_rolls_back_entire_new_agent_creation(api_key_client, workspace, payload, kind):
    project = Project.objects.create(workspace=workspace, name="Existing", identifier="EXIST")
    payload["project"]["existing_project_id"] = str(project.id)
    if kind == "module":
        Module.objects.create(project=project, name="Builds")
    else:
        page = Page.objects.create(workspace=workspace, owned_by=workspace.owner, name="Status")
        ProjectPage.objects.create(project=project, workspace=workspace, page=page)
    before = counts()
    response = api_key_client.put(url(workspace), payload, format="json")
    assert response.status_code == 409 and counts() == before
    project.refresh_from_db()
    assert project.external_source is None and project.external_id is None


def test_hermes_role_has_independent_stable_user_and_credential(api_key_client, workspace, payload):
    codex = provision(api_key_client, workspace, payload).data
    payload["agent"] = {"runtime": "hermes", "key": "rte", "display_name": "Hermes RTE"}
    hermes = provision(api_key_client, workspace, payload).data
    assert hermes["project"] == codex["project"]
    assert hermes["modules"] == codex["modules"] and hermes["pages"] == codex["pages"]
    assert hermes["agent"]["user_id"] != codex["agent"]["user_id"]
    assert hermes["credential"] != codex["credential"]
    assert api_key_client.get(url(workspace), {"runtime": "hermes", "agent_key": "rte"}).data == hermes


@pytest.mark.django_db(transaction=True)
def test_concurrent_provisioning_returns_one_project_user_and_token(api_token, workspace, payload):
    barrier = Barrier(2)

    def write(_):
        connections.close_all()
        try:
            client = APIClient()
            client.credentials(HTTP_X_API_KEY=api_token.token)
            barrier.wait(timeout=10)
            result = client.put(url(workspace), payload, format="json")
            return result.status_code, result.data
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, range(2)))
    assert sorted(code for code, _ in results) == [200, 201], results
    assert results[0][1] == results[1][1]
    assert Project.objects.count() == ProjectAgentRegistration.objects.count() == 1
    assert User.objects.filter(is_managed=True).count() == 1
    assert APIToken.objects.filter(is_service=True).count() == 1
    assert Module.objects.count() == Page.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_provisioning_serializes_with_legacy_chat_binding(api_key_client, api_token, workspace, payload, monkeypatch):
    data = provision(api_key_client, workspace, payload).data
    registration = ProjectAgentRegistration.objects.get(id=data["agent"]["registration_id"])
    old_revision = registration.revision
    payload["modules"].append({"key": "release", "name": "Release"})
    paused, release, binder_connected = Event(), Event(), Event()
    role = local()
    binder_pid = []
    original_save = ProjectAgentRegistration.save

    def save(instance, *args, **kwargs):
        if getattr(role, "provisioning", False):
            # Pause after registration was read, exactly where the stale save
            # used to overwrite a concurrently committed legacy chat binding.
            paused.set()
            assert release.wait(timeout=10)
        return original_save(instance, *args, **kwargs)

    monkeypatch.setattr(ProjectAgentRegistration, "save", save)

    def write(provisioning):
        connections.close_all()
        try:
            role.provisioning = provisioning
            client = APIClient()
            client.credentials(HTTP_X_API_KEY=api_token.token)
            if provisioning:
                response = client.put(url(workspace), payload, format="json")
            else:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    binder_pid.append(cursor.fetchone()[0])
                binder_connected.set()
                response = client.put(
                    f"/api/v1/workspaces/{workspace.slug}/projects/{data['project']['id']}/agent-registrations/codex/project/",
                    {
                        "display_name": "New chat",
                        "binding": {"binding_id": str(uuid4()), "thread_id": "new-chat"},
                        "setup": {"bundle_revision": "a" * 40, "skills": [], "mcp_servers": [], "verified_at": None},
                        "active": True,
                    },
                    format="json",
                    HTTP_IF_MATCH=f'"{old_revision}"',
                )
            return response.status_code, response.data
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        provisioned = executor.submit(write, True)
        assert paused.wait(timeout=10)
        bound = executor.submit(write, False)
        try:
            assert binder_connected.wait(timeout=10)
            deadline, blocked = monotonic() + 5, False
            while monotonic() < deadline and not bound.done():
                with connections["default"].cursor() as cursor:
                    cursor.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [binder_pid[0]])
                    row = cursor.fetchone()
                if row and row[0] == "Lock":
                    blocked = True
                    break
                sleep(0.01)
            assert blocked, "Legacy binding must wait for provisioning's project row lock"
        finally:
            release.set()
        assert provisioned.result(timeout=10)[0] == 200
        assert bound.result(timeout=10)[0] == 412
    registration.refresh_from_db()
    assert registration.binding == {} and registration.revision == old_revision + 1


def test_permission_workspace_scope_and_schema(api_key_client, api_token, workspace, create_user, payload):
    membership = WorkspaceMember.objects.get(workspace=workspace, member=create_user)
    membership.role = 15
    membership.save()
    assert api_key_client.put(url(workspace), payload, format="json").status_code == 403
    assert api_key_client.get(url(workspace)).status_code == 403
    membership.role = 20
    membership.save()
    other = Workspace.objects.create(name="Other workspace", slug="other", owner=create_user)
    api_token.workspace = other
    api_token.save()
    assert api_key_client.put(url(workspace), payload, format="json").status_code == 403
    api_token.workspace = workspace
    api_token.save()
    body = deepcopy(payload)
    body["agent"]["thread_id"] = "not-an-identity"
    assert api_key_client.put(url(workspace), body, format="json").status_code == 400
    assert api_key_client.put(url(workspace, "Bad Key"), payload, format="json").status_code == 400


@pytest.mark.parametrize("method", ["get", "put"])
def test_provisioning_logs_never_contain_request_or_response_secrets(monkeypatch, method):
    log = Mock()
    monkeypatch.setattr("plane.middleware.logger.process_logs.delay", log)
    factory = RequestFactory()
    request = getattr(factory, method)(
        "/api/v1/workspaces/example/project-provisioning/example/",
        data=json.dumps({"unexpected": "request-secret"}) if method == "put" else None,
        content_type="application/json",
        HTTP_X_API_KEY="header-secret",
    )
    response = HttpResponse(json.dumps({"credential": {"api_key": "response-secret"}}), content_type="application/json")
    middleware = APITokenLogMiddleware(lambda request: response)
    assert middleware(request) is response
    stored = log.call_args.kwargs["log_data"]
    serialized = json.dumps(stored)
    assert all(secret not in serialized for secret in ("request-secret", "response-secret", "header-secret"))
    assert stored["body"] == stored["response_body"] == "[REDACTED project provisioning]"


def test_missing_asset_get_is_conflict_instead_of_false_success(api_key_client, workspace, payload):
    data = provision(api_key_client, workspace, payload).data
    Page.objects.filter(id=data["pages"][0]["id"]).delete()
    assert api_key_client.get(url(workspace)).status_code == 409
    repaired = provision(api_key_client, workspace, payload).data
    assert repaired["agent"] == data["agent"] and repaired["credential"] == data["credential"]
    assert repaired["pages"][0]["id"] != data["pages"][0]["id"]
