# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Real PostgreSQL/API-key registry contracts, including competing writers."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import IntegrityError, connections, transaction
from rest_framework.test import APIClient

from plane.db.models import Page, Project, ProjectAgentRegistration, ProjectMember, User, Workspace, WorkspaceMember

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Registered agents", identifier="AGENT")
    ProjectMember.objects.create(project=project, workspace=workspace, member=create_user, role=20)
    return project


@pytest.fixture
def payload():
    return {
        "display_name": "Проектный агент",
        "binding": {"binding_id": str(uuid4()), "thread_id": str(uuid4()), "host_id": "local"},
        "setup": {
            "bundle_revision": "a" * 40,
            "skills": [{"id": "plane-management", "sha256": "b" * 64}],
            "mcp_servers": ["plane_lan", "hcom"],
            "verified_at": "2026-10-26T12:00:00+00:00",
        },
        "active": True,
    }


def registry_url(workspace, project):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/agent-registrations/"


def slot_url(workspace, project, runtime="codex", key="project"):
    return registry_url(workspace, project) + f"{runtime}/{key}/"


def put(client, url, payload, revision=0):
    return client.put(url, payload, format="json", HTTP_IF_MATCH=f'"{revision}"')


def test_create_get_list_noop_does_not_create_users_or_pages(api_key_client, workspace, project, payload):
    url = slot_url(workspace, project)
    counts = User.objects.count(), Page.objects.count(), ProjectMember.objects.count()
    assert api_key_client.get(url).status_code == 404
    assert api_key_client.get(registry_url(workspace, project)).data == []
    response = put(api_key_client, url, payload)
    assert response.status_code == 201, response.data
    assert response["ETag"] == '"1"'
    data = response.data
    assert set(data) == {
        "id",
        "project",
        "workspace",
        "runtime",
        "agent_key",
        "revision",
        "created_at",
        "updated_at",
        "display_name",
        "binding",
        "setup",
        "active",
    }
    assert data["project"] == str(project.id)
    assert data["workspace"] == str(workspace.id)
    assert data["runtime"] == "codex" and data["agent_key"] == "project"
    assert data["setup"]["verified_at"] == "2026-10-26T12:00:00Z"
    assert data["setup"]["mcp_servers"] == ["hcom", "plane_lan"]
    read = api_key_client.get(url)
    assert read.data == data and read["ETag"] == '"1"'
    noop = put(api_key_client, url, payload, revision=1)
    assert noop.status_code == 200 and noop.data == data
    assert api_key_client.get(registry_url(workspace, project)).data == [data]
    assert counts == (User.objects.count(), Page.objects.count(), ProjectMember.objects.count())
    assert ProjectAgentRegistration.objects.count() == 1


def test_rebind_deactivate_reactivate_preserves_slot_and_monotonic_revision(
    api_key_client, workspace, project, payload
):
    url = slot_url(workspace, project)
    first = put(api_key_client, url, payload).data
    payload["binding"] = {"binding_id": str(uuid4()), "thread_id": str(uuid4())}
    second = put(api_key_client, url, payload, revision=1)
    assert second.status_code == 200
    assert second.data["id"] == first["id"] and second.data["revision"] == 2
    assert second.data["created_at"] == first["created_at"]
    assert second.data["binding"] == payload["binding"]
    payload["active"] = False
    third = put(api_key_client, url, payload, revision=2)
    assert third.status_code == 200 and third.data["revision"] == 3 and third.data["active"] is False
    assert api_key_client.get(registry_url(workspace, project)).data == [third.data]
    assert put(api_key_client, url, payload, revision=0).status_code == 412
    payload["active"] = True
    fourth = put(api_key_client, url, payload, revision=3)
    assert fourth.data["revision"] == 4 and fourth.data["id"] == first["id"]
    stale = put(api_key_client, url, payload, revision=1)
    assert stale.status_code == 412
    assert api_key_client.get(url).data == fourth.data


def test_database_uniqueness_includes_inactive_rows(project, payload):
    payload["active"] = False
    ProjectAgentRegistration.objects.create(project=project, runtime="codex", agent_key="project", **payload)
    with pytest.raises(IntegrityError), transaction.atomic():
        ProjectAgentRegistration.objects.create(project=project, runtime="codex", agent_key="project", **payload)
    assert ProjectAgentRegistration.objects.count() == 1
    assert "deleted_at" not in {field.name for field in ProjectAgentRegistration._meta.fields}


def test_hermes_role_slots_and_project_identity_are_independent(
    api_key_client, workspace, project, create_user, payload
):
    codex = put(api_key_client, slot_url(workspace, project), payload)
    payload["binding"] = {"binding_id": str(uuid4()), "agent_id": "rte", "session_id": "s1", "host_id": "proxmox"}
    rte = put(api_key_client, slot_url(workspace, project, "hermes", "rte"), payload)
    payload["binding"]["agent_id"] = "reviewer"
    reviewer = put(api_key_client, slot_url(workspace, project, "hermes", "reviewer"), payload)
    assert codex.status_code == rte.status_code == reviewer.status_code == 201
    other = Project.objects.create(workspace=workspace, name="Second agents", identifier="AGENT2")
    ProjectMember.objects.create(project=other, workspace=workspace, member=create_user, role=15)
    another = put(api_key_client, slot_url(workspace, other, "hermes", "rte"), payload)
    assert another.status_code == 201 and another.data["id"] != rte.data["id"]
    listing = api_key_client.get(registry_url(workspace, project)).data
    assert [(item["runtime"], item["agent_key"]) for item in listing] == [
        ("codex", "project"),
        ("hermes", "reviewer"),
        ("hermes", "rte"),
    ]


def test_precondition_required_and_stale_writes_do_not_mutate(api_key_client, workspace, project, payload):
    url = slot_url(workspace, project)
    assert api_key_client.put(url, payload, format="json").status_code == 428
    assert put(api_key_client, url, payload, revision=1).status_code == 412
    assert not ProjectAgentRegistration.objects.exists()
    created = put(api_key_client, url, payload)
    payload["display_name"] = "Stale overwrite"
    assert put(api_key_client, url, payload).status_code == 412
    assert api_key_client.get(url).data == created.data


@pytest.mark.parametrize("header", ["*", "0", '"01"', '"-1"', '"1", "2"', '"not-a-revision"', ""])
def test_invalid_precondition_rejected(api_key_client, workspace, project, payload, header):
    response = api_key_client.put(slot_url(workspace, project), payload, format="json", HTTP_IF_MATCH=header)
    assert response.status_code == 400
    assert not ProjectAgentRegistration.objects.exists()


@pytest.mark.parametrize(
    "runtime,key",
    [
        ("claude", "project"),
        ("Codex", "project"),
        ("codex", "rte"),
        ("codex", "PROJECT"),
        ("hermes", "RTE"),
        ("hermes", "x" * 65),
    ],
)
def test_invalid_slots_rejected(api_key_client, workspace, project, payload, runtime, key):
    assert put(api_key_client, slot_url(workspace, project, runtime, key), payload).status_code == 400
    assert not ProjectAgentRegistration.objects.exists()


@pytest.mark.parametrize(
    "path,value",
    [
        (("token",), "credential-must-not-be-stored"),
        (("binding", "api_key"), "credential-must-not-be-stored"),
        (("setup", "credentials"), {"key": "credential-must-not-be-stored"}),
        (("display_name",), 42),
        (("display_name",), "x" * 256),
        (("active",), "true"),
        (("binding",), []),
        (("binding", "binding_id"), "not-a-uuid"),
        (("binding", "binding_id"), 123),
        (("binding", "thread_id"), 123),
        (("binding", "thread_id"), "line\nbreak"),
        (("binding", "session_id"), "hermes-only"),
        (("setup", "bundle_revision"), "a79090e"),
        (("setup", "bundle_revision"), "A" * 40),
        (("setup", "verified_at"), "2026-10-26T12:00:00"),
        (("setup", "verified_at"), "2026-10-26T12:00:00+03:00"),
        (("setup", "verified_at"), "2026-99-99T12:00:00Z"),
        (("setup", "mcp_servers"), ["plane_lan", "plane_lan"]),
        (("setup", "mcp_servers"), ["server"] * 65),
        (("setup", "mcp_servers"), ["https://server?token=secret"]),
        (("setup", "skills"), [{"id": "plane", "sha256": "bad"}]),
        (("setup", "skills"), [{"id": "plane", "sha256": "a" * 64, "token": "secret"}]),
        (("setup", "skills"), [{"id": "plane", "sha256": "a" * 64}] * 2),
        (("setup", "skills"), [{"id": f"skill-{i}", "sha256": "a" * 64} for i in range(129)]),
    ],
)
def test_bounded_schema_rejects_invalid_or_credential_fields(api_key_client, workspace, project, payload, path, value):
    target = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    response = put(api_key_client, slot_url(workspace, project), payload)
    assert response.status_code == 400, response.data
    assert b"credential-must-not-be-stored" not in response.content
    assert not ProjectAgentRegistration.objects.exists()


@pytest.mark.parametrize(
    "path",
    [
        ("binding", "thread_id"),
        ("binding", "binding_id"),
        ("setup", "bundle_revision"),
        ("setup", "verified_at"),
        ("display_name",),
    ],
)
def test_required_fields(api_key_client, workspace, project, payload, path):
    target = payload
    for part in path[:-1]:
        target = target[part]
    del target[path[-1]]
    assert put(api_key_client, slot_url(workspace, project), payload).status_code == 400


@pytest.mark.parametrize("payload", [[], {"unexpected": "x" * 65536}])
def test_invalid_outer_object_or_oversized_json_is_not_a_server_error(api_key_client, workspace, project, payload):
    response = put(api_key_client, slot_url(workspace, project), payload)
    assert response.status_code == 400
    assert not ProjectAgentRegistration.objects.exists()


def test_default_active_nullable_verification_and_compressed_etag(api_key_client, workspace, project, payload):
    del payload["active"]
    payload["setup"]["verified_at"] = None
    payload["setup"]["bundle_revision"] = "c" * 64
    url = slot_url(workspace, project)
    created = put(api_key_client, url, payload)
    assert created.status_code == 201 and created.data["active"] is True
    assert created.data["setup"]["verified_at"] is None
    read = api_key_client.get(url, HTTP_ACCEPT_ENCODING="gzip")
    assert read["ETag"].startswith('W/"')
    updated = api_key_client.put(url, payload, format="json", HTTP_IF_MATCH=read["ETag"])
    assert updated.status_code == 200 and updated.data == created.data


def test_only_get_and_conditional_put_are_exposed(api_key_client, workspace, project, payload):
    url = slot_url(workspace, project)
    assert put(api_key_client, url, payload).status_code == 201
    assert api_key_client.delete(url).status_code == 405
    assert api_key_client.post(url, payload, format="json").status_code == 405
    assert api_key_client.patch(url, {"active": False}, format="json").status_code == 405
    assert put(api_key_client, registry_url(workspace, project), payload).status_code == 405
    assert ProjectAgentRegistration.objects.count() == 1


def test_api_key_required_and_workspace_scope_enforced(
    api_key_client, api_client, api_token, workspace, project, create_user, payload
):
    url = slot_url(workspace, project)
    anonymous = APIClient()
    assert anonymous.get(url).status_code in (401, 403)
    anonymous.force_login(create_user)
    assert anonymous.get(url).status_code in (401, 403)
    other = Workspace.objects.create(name="Other registration scope", slug="registration-other", owner=create_user)
    WorkspaceMember.objects.create(workspace=other, member=create_user, role=20)
    other_project = Project.objects.create(workspace=other, name="Other registration", identifier="OTHER")
    ProjectMember.objects.create(project=other_project, workspace=other, member=create_user, role=20)
    api_token.workspace = workspace
    api_token.save()
    assert put(api_key_client, slot_url(other, other_project), payload).status_code == 403
    assert api_key_client.get(registry_url(other, other_project)).status_code == 403
    assert put(api_key_client, slot_url(workspace, other_project), payload).status_code == 403
    assert not ProjectAgentRegistration.objects.exists()


def test_project_member_can_write_guest_can_only_read_and_removed_member_cannot_read(
    api_key_client, workspace, project, create_user, payload
):
    member = ProjectMember.objects.get(project=project, member=create_user)
    member.role = 15
    member.save()
    url = slot_url(workspace, project)
    assert put(api_key_client, url, payload).status_code == 201
    member.role = 5
    member.save()
    assert api_key_client.get(url).status_code == 200
    assert put(api_key_client, url, payload, revision=1).status_code == 403
    member.is_active = False
    member.save()
    assert api_key_client.get(url).status_code == 403
    assert api_key_client.get(registry_url(workspace, project)).status_code == 403


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("existing", [False, True])
def test_concurrent_create_or_rebind_has_exactly_one_winner(
    api_token, api_key_client, workspace, project, payload, existing
):
    url = slot_url(workspace, project)
    initial = put(api_key_client, url, payload).data if existing else None
    barrier = Barrier(2)
    payloads = [deepcopy(payload), deepcopy(payload)]
    for index, body in enumerate(payloads):
        body["display_name"] = f"Competing writer {index}"
        body["binding"] = {"binding_id": str(uuid4()), "thread_id": str(uuid4())}

    def write(body):
        connections.close_all()
        try:
            client = APIClient()
            client.credentials(HTTP_X_API_KEY=api_token.token)
            barrier.wait(timeout=10)
            response = put(client, url, body, revision=1 if existing else 0)
            return response.status_code, response.data
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(write, payloads))
    assert sorted(code for code, _ in results) == [200 if existing else 201, 412], results
    assert ProjectAgentRegistration.objects.count() == 1
    saved = api_key_client.get(url).data
    winner = next(data for code, data in results if code < 300)
    assert saved == winner
    assert saved["revision"] == (2 if existing else 1)
    if initial:
        assert saved["id"] == initial["id"]
