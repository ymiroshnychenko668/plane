"""Assignment routing and execution claims use real PostgreSQL arbitration."""

import base64
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import connections
from rest_framework.test import APIClient

from plane.db.models import (
    Issue,
    IssueAssignee,
    Page,
    Project,
    ProjectAgentRegistration,
    ProjectAssignmentRoute,
    ProjectDispatchServiceGrant,
    ProjectMember,
    ProjectPage,
    ProjectTeamContact,
    State,
    User,
    WorkItemExecution,
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
    value = Project.objects.create(workspace=workspace, name="Assignment dispatch", identifier="DISPATCH")
    ProjectMember.objects.create(project=value, member=create_user, role=20)
    return value


@pytest.fixture
def actor(project, create_user):
    return ProjectAgentRegistration.objects.create(
        project=project,
        runtime="codex",
        agent_key="project",
        display_name="Project agent",
        user=create_user,
        binding={"binding_id": str(uuid4()), "thread_id": str(uuid4()), "host_id": "local"},
        provisioning={"role": "Implementation"},
    )


@pytest.fixture
def contact(actor, project):
    return ProjectTeamContact.objects.create(
        project=project,
        registration=actor,
        address="agent",
        target={key: value for key, value in actor.binding.items() if key in {"thread_id", "host_id"}},
    )


@pytest.fixture
def issue(project, actor, contact):
    state = State.objects.create(project=project, name="Todo", group="unstarted")
    value = Issue.objects.create(project=project, state=state, name="Implement assignment dispatch")
    IssueAssignee.objects.create(project=project, issue=value, assignee=actor.user)
    ProjectAssignmentRoute.objects.create(project=project, user=actor.user, contact=contact)
    return value


def url(project, suffix):
    return f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.id}/{suffix}"


def route_url(project, user_id=None):
    return url(project, "assignment-routes/" + (f"{user_id}/" if user_id else ""))


def execution_url(project, issue, operation=None):
    return url(project, f"work-items/{issue.id}/execution/" + (f"{operation}/" if operation else ""))


def claim(client, project, issue, actor, run_id="run-1", request_id="request-1"):
    return client.post(
        execution_url(project, issue, "claim"),
        {"registration_id": str(actor.id), "run_id": run_id, "request_id": request_id},
        format="json",
    )


def checkpoint(client, project, issue, actor, revision, operation="checkpoint", **changes):
    return client.post(
        execution_url(project, issue, operation),
        {
            "registration_id": str(actor.id),
            "run_id": "run-1",
            "checkpoint": {"phase": "review"},
            "status": "paused",
            **changes,
        },
        format="json",
        HTTP_IF_MATCH=f'"{revision}"',
    )


def grant_payload(user_id, **changes):
    return {
        "user_id": str(user_id),
        "sender_address": "plane-dispatch",
        "public_key": base64.b64encode(b"p" * 32).decode(),
        **changes,
    }


def test_explicit_route_cas_noop_and_deactivation(api_key_client, project, actor, contact):
    address = route_url(project, actor.user_id)
    payload = {"contact_id": str(contact.id)}
    assert api_key_client.get(address).status_code == 404
    assert api_key_client.get(route_url(project)).data == []
    assert api_key_client.put(address, payload, format="json").status_code == 428
    first = api_key_client.put(address, payload, format="json", HTTP_IF_MATCH='"0"')
    assert first.status_code == 201, first.data
    assert first["ETag"] == '"1"' and first.data["available"]
    assert first.data["registration_id"] == str(actor.id)
    assert api_key_client.get(route_url(project)).data == [first.data]
    assert api_key_client.put(address, payload, format="json", HTTP_IF_MATCH='"0"').status_code == 412
    assert api_key_client.put(address, payload, format="json", HTTP_IF_MATCH='"1"').data == first.data
    removed = api_key_client.delete(address, HTTP_IF_MATCH='"1"')
    assert removed.status_code == 200 and removed.data["revision"] == 2
    assert not removed.data["active"] and not removed.data["available"]
    assert ProjectAssignmentRoute.objects.count() == 1


@pytest.mark.parametrize("change", ["contact", "identity", "binding", "membership", "workspace_membership"])
def test_route_availability_tracks_canonical_team_changes(api_key_client, project, actor, contact, change):
    ProjectAssignmentRoute.objects.create(project=project, user=actor.user, contact=contact)
    if change == "contact":
        contact.active = False
        contact.save()
    elif change == "identity":
        actor.active = False
        actor.save()
    elif change == "binding":
        actor.binding = {**actor.binding, "thread_id": str(uuid4())}
        actor.save()
    else:
        model = ProjectMember if change == "membership" else WorkspaceMember
        model.objects.filter(member=actor.user).update(is_active=False)
        reader = User.objects.create(username=uuid4().hex, email=f"reader-{uuid4()}@test.invalid")
        WorkspaceMember.objects.create(workspace=project.workspace, member=reader, role=15)
        ProjectMember.objects.create(project=project, member=reader, role=15)
        api_key_client.force_authenticate(user=reader)
    read = api_key_client.get(route_url(project, actor.user_id))
    assert read.status_code == 200 and not read.data["available"]


def test_route_admin_scope_identity_and_migration_guards(api_key_client, project, actor, contact):
    payload = {"contact_id": str(contact.id)}
    address = route_url(project, actor.user_id)
    ProjectMember.objects.filter(project=project, member=actor.user).update(role=15)
    assert api_key_client.put(address, payload, format="json", HTTP_IF_MATCH='"0"').status_code == 403
    assert api_key_client.get(route_url(project)).status_code == 200
    ProjectMember.objects.filter(project=project, member=actor.user).update(role=20)
    assert (
        api_key_client.put(route_url(project, uuid4()), payload, format="json", HTTP_IF_MATCH='"0"').status_code == 409
    )
    page = Page.objects.create(workspace=project.workspace, owned_by=actor.user, name="hcom team")
    ProjectPage.objects.create(project=project, page=page, workspace=project.workspace)
    assert api_key_client.put(address, payload, format="json", HTTP_IF_MATCH='"0"').status_code == 409


def test_shared_identity_routes_choose_one_explicit_chat(api_key_client, project, actor, contact):
    other = ProjectAgentRegistration.objects.create(
        project=project,
        runtime="codex",
        agent_key="reviewer",
        user=actor.user,
        binding={"thread_id": str(uuid4())},
    )
    other_contact = ProjectTeamContact.objects.create(
        project=project, registration=other, address="review", target=other.binding
    )
    address = route_url(project, actor.user_id)
    first = api_key_client.put(address, {"contact_id": str(contact.id)}, format="json", HTTP_IF_MATCH='"0"')
    second = api_key_client.put(address, {"contact_id": str(other_contact.id)}, format="json", HTTP_IF_MATCH='"1"')
    assert first.status_code == 201 and second.status_code == 200
    assert second.data["registration_id"] == str(other.id) and second.data["revision"] == 2
    assert ProjectAssignmentRoute.objects.filter(project=project, user=actor.user).count() == 1


def test_service_grant_is_admin_bound_public_material(api_key_client, project, actor):
    address = url(project, "dispatch-service-grants/")
    payload = grant_payload(actor.user_id)
    assert api_key_client.post(address, payload, format="json").status_code == 428
    created = api_key_client.post(address, payload, format="json", HTTP_IF_MATCH='"0"')
    assert created.status_code == 201, created.data
    assert created.data["available"] and created.data["public_key"] == payload["public_key"]
    detail = address + created.data["id"] + "/"
    assert api_key_client.get(detail).data == created.data
    assert api_key_client.post(address, payload, format="json", HTTP_IF_MATCH='"0"').status_code == 409
    assert (
        api_key_client.put(
            detail, {**payload, "sender_address": "other"}, format="json", HTTP_IF_MATCH='"1"'
        ).status_code
        == 409
    )
    stopped = api_key_client.put(detail, {**payload, "active": False}, format="json", HTTP_IF_MATCH='"1"')
    assert stopped.status_code == 200 and stopped.data["revision"] == 2 and not stopped.data["available"]
    ProjectMember.objects.filter(project=project, member=actor.user).update(role=15)
    assert api_key_client.get(address).status_code == 200
    assert api_key_client.put(detail, payload, format="json", HTTP_IF_MATCH='"2"').status_code == 403
    assert ProjectDispatchServiceGrant.objects.count() == 1


def test_grant_rejects_unscoped_principal_and_agent_address(api_key_client, project, actor, contact):
    address = url(project, "dispatch-service-grants/")
    assert api_key_client.post(address, grant_payload(uuid4()), format="json", HTTP_IF_MATCH='"0"').status_code == 409
    assert (
        api_key_client.post(
            address, grant_payload(actor.user_id, sender_address=contact.address), format="json", HTTP_IF_MATCH='"0"'
        ).status_code
        == 409
    )
    assert (
        api_key_client.post(
            address, grant_payload(actor.user_id, public_key="invalid"), format="json", HTTP_IF_MATCH='"0"'
        ).status_code
        == 400
    )


def test_execution_claim_checkpoint_release_and_resume(api_key_client, project, actor, issue):
    address = execution_url(project, issue)
    assert api_key_client.get(address).data == {
        "work_item_id": str(issue.id),
        "registration_id": None,
        "user_id": None,
        "run_id": None,
        "request_id": None,
        "status": "idle",
        "checkpoint": {},
        "revision": 0,
        "stop_requested": False,
        "stop_reasons": [],
    }
    started = claim(api_key_client, project, issue, actor)
    assert started.status_code == 201 and started.data["revision"] == 1, started.data
    assert claim(api_key_client, project, issue, actor).data == started.data
    assert claim(api_key_client, project, issue, actor, run_id="competing").status_code == 409
    saved = checkpoint(api_key_client, project, issue, actor, 1)
    assert saved.status_code == 200 and saved.data["revision"] == 2
    assert checkpoint(api_key_client, project, issue, actor, 1).status_code == 412
    released = checkpoint(api_key_client, project, issue, actor, 2, operation="release")
    assert released.status_code == 200 and released.data["registration_id"] is None
    assert released.data["checkpoint"] == {"phase": "review"}
    assert claim(api_key_client, project, issue, actor).status_code == 409
    resumed = claim(api_key_client, project, issue, actor, run_id="run-2", request_id="request-2")
    assert resumed.status_code == 200 and resumed.data["checkpoint"] == {"phase": "review"}
    assert resumed.data["revision"] == 4
    assert checkpoint(api_key_client, project, issue, actor, 4, operation="release", run_id="run-2").status_code == 200
    # The old request remains a tombstone even after a newer run released.
    assert claim(api_key_client, project, issue, actor).status_code == 409


def test_execution_owner_can_release_after_unassignment(api_key_client, project, actor, issue):
    assert claim(api_key_client, project, issue, actor).status_code == 201
    IssueAssignee.objects.filter(issue=issue).delete()
    read = api_key_client.get(execution_url(project, issue))
    assert read.data["stop_requested"] and read.data["stop_reasons"] == ["unassigned"]
    assert (
        checkpoint(api_key_client, project, issue, actor, 1, operation="release", status="stopped").status_code == 200
    )
    assert claim(api_key_client, project, issue, actor, run_id="run-2", request_id="request-2").status_code == 409


@pytest.mark.parametrize("group", ["completed", "cancelled"])
def test_execution_closure_requests_stop_without_transferring_owner(api_key_client, project, actor, issue, group):
    assert claim(api_key_client, project, issue, actor).status_code == 201
    State.objects.filter(id=issue.state_id).update(group=group)
    read = api_key_client.get(execution_url(project, issue))
    assert read.data["stop_requested"] and group in read.data["stop_reasons"]
    assert read.data["registration_id"] == str(actor.id)
    assert (
        checkpoint(api_key_client, project, issue, actor, 1, operation="release", status="stopped").status_code == 200
    )
    assert claim(api_key_client, project, issue, actor, run_id="run-2", request_id="request-2").status_code == 409


def test_execution_cannot_impersonate_other_agent_or_steal_after_reassignment(api_key_client, project, actor, issue):
    user = User.objects.create(username=uuid4().hex, email=f"other-{uuid4()}@test.invalid")
    WorkspaceMember.objects.create(workspace=project.workspace, member=user, role=15)
    ProjectMember.objects.create(project=project, member=user, role=15)
    other = ProjectAgentRegistration.objects.create(project=project, runtime="codex", agent_key="other", user=user)
    assert claim(api_key_client, project, issue, other).status_code == 403
    assert claim(api_key_client, project, issue, actor).status_code == 201
    IssueAssignee.objects.filter(issue=issue).delete()
    IssueAssignee.objects.create(project=project, issue=issue, assignee=user)
    api_key_client.force_authenticate(user=user)
    assert (
        claim(api_key_client, project, issue, other, run_id="other-run", request_id="other-request").status_code == 409
    )
    assert checkpoint(api_key_client, project, issue, other, 1, operation="release").status_code == 409
    assert WorkItemExecution.objects.get(issue=issue).registration_id == actor.id


def test_execution_checkpoint_requires_cas_and_valid_operation_status(api_key_client, project, actor, issue):
    assert claim(api_key_client, project, issue, actor).status_code == 201
    payload = {"registration_id": str(actor.id), "run_id": "run-1", "checkpoint": {}, "status": "running"}
    assert api_key_client.post(execution_url(project, issue, "checkpoint"), payload, format="json").status_code == 428
    assert checkpoint(api_key_client, project, issue, actor, 1, status="completed").status_code == 400
    assert (
        checkpoint(api_key_client, project, issue, actor, 1, operation="release", status="running").status_code == 400
    )
    assert checkpoint(api_key_client, project, issue, actor, 1, checkpoint=[]).status_code == 400


def test_execution_requires_available_primary_receiver(api_key_client, project, actor, issue):
    route = ProjectAssignmentRoute.objects.get(project=project, user=actor.user)
    route.active = False
    route.save()
    assert claim(api_key_client, project, issue, actor).status_code == 409
    route.delete()
    assert claim(api_key_client, project, issue, actor).status_code == 409
    assert not WorkItemExecution.objects.filter(issue=issue).exists()


def test_primary_chat_change_requests_stop_then_allows_checkpointed_handoff(api_key_client, project, actor, issue):
    second = ProjectAgentRegistration.objects.create(
        project=project,
        runtime="codex",
        agent_key="second-chat",
        user=actor.user,
        binding={"thread_id": str(uuid4())},
    )
    second_contact = ProjectTeamContact.objects.create(
        project=project, registration=second, address="second", target=second.binding
    )
    assert claim(api_key_client, project, issue, second).status_code == 409
    assert claim(api_key_client, project, issue, actor).status_code == 201
    ProjectAssignmentRoute.objects.filter(project=project, user=actor.user).update(contact=second_contact)
    read = api_key_client.get(execution_url(project, issue))
    assert read.data["stop_requested"] and read.data["stop_reasons"] == ["route_changed"]
    assert claim(api_key_client, project, issue, second, run_id="run-2", request_id="request-2").status_code == 409
    released = checkpoint(api_key_client, project, issue, actor, 1, operation="release", status="stopped")
    assert released.status_code == 200
    resumed = claim(api_key_client, project, issue, second, run_id="run-2", request_id="request-2")
    assert resumed.status_code == 200 and resumed.data["checkpoint"] == {"phase": "review"}


def test_unavailable_primary_route_requests_stop_without_owner_expiry(api_key_client, project, actor, issue):
    assert claim(api_key_client, project, issue, actor).status_code == 201
    ProjectAssignmentRoute.objects.filter(project=project, user=actor.user).update(active=False)
    read = api_key_client.get(execution_url(project, issue))
    assert read.data["stop_requested"] and read.data["stop_reasons"] == ["route_unavailable"]
    assert read.data["registration_id"] == str(actor.id)


def test_cross_project_routes_grants_and_executions_are_not_readable(api_key_client, project, actor, issue):
    other = Project.objects.create(workspace=project.workspace, name="Other", identifier="OTHER")
    assert api_key_client.get(route_url(other)).status_code == 403
    assert api_key_client.get(url(other, "dispatch-service-grants/")).status_code == 403
    ProjectMember.objects.create(project=other, member=actor.user, role=15)
    assert api_key_client.get(execution_url(other, issue)).status_code == 404
    assert claim(api_key_client, other, issue, actor).status_code == 404


def test_service_sender_address_cannot_be_taken_by_agent_contact(api_key_client, project, actor):
    created = api_key_client.post(
        url(project, "dispatch-service-grants/"), grant_payload(actor.user_id), format="json", HTTP_IF_MATCH='"0"'
    )
    assert created.status_code == 201
    result = api_key_client.put(
        url(project, "team/contacts/plane-dispatch/"),
        {"registration_id": str(actor.id), "target": {"thread_id": actor.binding["thread_id"], "host_id": "local"}},
        format="json",
        HTTP_IF_MATCH='"0"',
    )
    assert result.status_code == 409


@pytest.mark.django_db(transaction=True)
def test_concurrent_execution_claims_have_one_owner(api_token, project, actor, issue):
    barrier = Barrier(2)

    def publish(number):
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            barrier.wait(timeout=10)
            return claim(
                client, project, issue, actor, run_id=f"run-{number}", request_id=f"request-{number}"
            ).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(publish, range(2)))
    assert sorted(statuses) == [201, 409]
    assert WorkItemExecution.objects.filter(issue=issue).count() == 1
