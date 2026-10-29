"""Teams persistence, permissions and legacy-address migration contracts."""

import html
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event
from uuid import uuid4
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.db import connections
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from plane.db.models import (
    Page,
    Project,
    ProjectAgentRegistration,
    ProjectMember,
    ProjectPage,
    ProjectTeamContact,
    ProjectTeamMigration,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.utils.team import parse_document

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Team registry", identifier="TEAM")
    ProjectMember.objects.create(project=project, member=create_user, role=20)
    return project


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


def url(project, suffix="", api=True):
    return f"/api/{'v1/' if api else ''}workspaces/{project.workspace.slug}/projects/{project.id}/team/{suffix}"


def contact(client, project, actor, address="agent", revision=0, **changes):
    payload = {
        "registration_id": str(actor.id),
        "target": {k: v for k, v in actor.binding.items() if k in {"thread_id", "host_id", "agent_id", "profile"}},
        **changes,
    }
    return client.put(url(project, f"contacts/{address}/"), payload, format="json", HTTP_IF_MATCH=f'"{revision}"')


def legacy_page(project, user, actor, contacts=None):
    value = {
        "kind": "agent-platform-hcom-team",
        "schema_version": 1,
        "workspace_slug": project.workspace.slug,
        "project_id": str(project.id),
        "contacts": contacts
        if contacts is not None
        else {
            "agent": {
                "address": "agent",
                "runtime": actor.runtime,
                "agent_key": actor.agent_key,
                "registration_id": str(actor.id),
                "user_id": str(actor.user_id),
                "target": {k: v for k, v in actor.binding.items() if k in {"thread_id", "host_id"}},
            }
        },
    }
    page = Page.objects.create(
        workspace=project.workspace,
        owned_by=user,
        name="hcom team",
        description_html="<p>Keep this note.</p><pre><code>" + html.escape(json.dumps(value)) + "</code></pre>",
    )
    ProjectPage.objects.create(project=project, page=page, workspace=project.workspace)
    return page


def test_roster_session_api_persistence_and_capability(api_key_client, session_client, actor, project):
    created = contact(api_key_client, project, actor)
    assert created.status_code == 201, created.data
    assert created.data["available"] and created["ETag"] == '"1"'
    roster = api_key_client.get(url(project))
    session = session_client.get(url(project, api=False))
    assert roster.status_code == session.status_code == 200
    assert roster.data == session.data
    assert roster.data["actors"][0]["registration_id"] == str(actor.id)
    assert roster.data["members"][0]["id"] == str(actor.user_id)
    assert "api_token" not in json.dumps(roster.data) and "credential" not in json.dumps(roster.data)
    assert roster.data["legacy"] == {"status": "none", "page_id": None}
    resolved = api_key_client.post(url(project, "resolve/"), {"address": "@agent"}, format="json")
    assert resolved.data == created.data
    assert ProjectTeamContact.objects.get(project=project).target == created.data["target"]
    assert (
        "teams"
        in api_key_client.get(f"/api/v1/workspaces/{project.workspace.slug}/management-capabilities/").data[
            "capabilities"
        ]
    )


def test_contact_cas_idempotence_and_tombstone(api_key_client, actor, project):
    assert contact(api_key_client, project, actor).status_code == 201
    assert contact(api_key_client, project, actor, revision=0).status_code == 412
    assert contact(api_key_client, project, actor, revision=1).data["revision"] == 1
    assert api_key_client.put(url(project, "contacts/agent/"), {}, format="json").status_code == 428
    removed = api_key_client.delete(url(project, "contacts/agent/"), HTTP_IF_MATCH='"1"')
    assert removed.status_code == 200 and removed.data["revision"] == 2 and not removed.data["available"]
    assert api_key_client.post(url(project, "resolve/"), {"address": "agent"}, format="json").status_code == 404
    assert contact(api_key_client, project, actor, revision=0).status_code == 412
    assert contact(api_key_client, project, actor, revision=2).data["available"]


@pytest.mark.parametrize("change", ["registration", "membership", "workspace_membership", "target", "user"])
def test_identity_changes_immediately_invalidate_resolution(api_key_client, actor, project, change, create_user):
    assert contact(api_key_client, project, actor).status_code == 201
    if change == "registration":
        actor.active = False
        actor.save()
    elif change == "membership":
        ProjectMember.objects.filter(project=project, member=actor.user).update(is_active=False)
    elif change == "workspace_membership":
        WorkspaceMember.objects.filter(workspace=project.workspace, member=actor.user).update(is_active=False)
    elif change == "user":
        User.objects.filter(id=actor.user_id).update(is_active=False)
    else:
        actor.binding = {**actor.binding, "thread_id": str(uuid4())}
        actor.save()
    # Use an independent admin so changing the target identity cannot hide failures behind caller auth.
    reader = User.objects.create(username=uuid4().hex, email=f"reader-{uuid4()}@test.invalid", display_name="Reader")
    ProjectMember.objects.create(project=project, member=reader, role=20)
    WorkspaceMember.objects.create(workspace=project.workspace, member=reader, role=20)
    api_key_client.force_authenticate(user=reader)
    result = api_key_client.post(url(project, "resolve/"), {"address": "agent"}, format="json")
    assert result.status_code == 404, result.data
    assert api_key_client.get(url(project)).data["contacts"][0]["available"] is False


def test_scope_permissions_and_target_guards(api_key_client, actor, project, create_user, api_token):
    other = Project.objects.create(workspace=project.workspace, name="Other", identifier="OTHER")
    foreign = ProjectAgentRegistration.objects.create(
        project=other, runtime="codex", agent_key="project", user=create_user
    )
    assert contact(api_key_client, project, foreign).status_code == 404
    assert api_key_client.get(url(other)).status_code == 403
    assert (
        contact(api_key_client, project, actor, target={"thread_id": str(uuid4()), "host_id": "local"}).status_code
        == 409
    )
    assert (
        contact(
            api_key_client, project, actor, target={"thread_id": actor.binding["thread_id"], "api_key": "forbidden"}
        ).status_code
        == 400
    )
    assert contact(api_key_client, project, actor).status_code == 201
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    assert api_key_client.get(url(project)).status_code == 200
    assert contact(api_key_client, project, actor, revision=1).status_code == 403
    assert api_key_client.post(url(project, "resolve/"), {"address": "agent"}, format="json").status_code == 200
    elsewhere = Workspace.objects.create(name="Elsewhere", slug="elsewhere", owner=create_user)
    WorkspaceMember.objects.create(workspace=elsewhere, member=create_user, role=20)
    elsewhere_project = Project.objects.create(workspace=elsewhere, name="Elsewhere", identifier="ELSE")
    ProjectMember.objects.create(project=elsewhere_project, member=create_user, role=20)
    api_token.workspace = project.workspace
    api_token.save()
    assert api_key_client.get(url(elsewhere_project)).status_code == 403


def test_member_cannot_manage_others_but_owner_can_edit_role(api_key_client, actor, project, create_user):
    stranger = User.objects.create(username=uuid4().hex, email="stranger@test.invalid", display_name="Stranger")
    WorkspaceMember.objects.create(workspace=project.workspace, member=stranger, role=15)
    ProjectMember.objects.create(project=project, member=stranger, role=15)
    other = ProjectAgentRegistration.objects.create(
        project=project, runtime="codex", agent_key="other", user=stranger, binding=actor.binding
    )
    ProjectMember.objects.filter(project=project, member=create_user).update(role=15)
    assert contact(api_key_client, project, other).status_code == 403
    result = api_key_client.patch(
        url(project, f"agents/{actor.id}/"),
        {"display_name": "Renamed", "role": "Review", "active": False},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    assert result.status_code == 200, result.data
    assert result.data["revision"] == 2 and result.data["role"] == "Review"
    assert (
        api_key_client.patch(
            url(project, f"agents/{actor.id}/"), {"role": "Stale"}, format="json", HTTP_IF_MATCH='"1"'
        ).status_code
        == 412
    )
    assert (
        api_key_client.patch(
            url(project, f"agents/{other.id}/"), {"role": "Take over"}, format="json", HTTP_IF_MATCH='"1"'
        ).status_code
        == 403
    )


def test_import_atomic_and_legacy_projection_uses_registry(api_key_client, actor, project, create_user):
    page = legacy_page(project, create_user, actor)
    source = page.description_html
    assert api_key_client.get(url(project)).data["legacy"]["status"] == "pending"
    assert contact(api_key_client, project, actor).status_code == 409
    result = api_key_client.post(url(project, "import/"), {}, format="json")
    assert result.status_code == 200 and result.data["imported"] == 1, result.data
    assert api_key_client.post(url(project, "import/"), {}, format="json").data["changed"] is False
    page.refresh_from_db()
    assert page.description_html == source and page.is_locked
    path = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.id}/pages/{page.id}/description/"
    result = api_key_client.get(path)
    assert result.status_code == 200, result.data
    assert result.data["source"] == "teams" and "Keep this note." in result.data["description_html"]
    assert parse_document(result.data["description_html"], project)[0]["contacts"]["agent"]["user_id"] == str(
        actor.user_id
    )
    etag = result["ETag"]
    assert api_key_client.delete(url(project, "contacts/agent/"), HTTP_IF_MATCH='"1"').status_code == 200
    result = api_key_client.get(path)
    assert result["ETag"] != etag
    assert parse_document(result.data["description_html"], project)[0]["contacts"] == {}
    page.refresh_from_db()
    assert page.description_html == source


@pytest.mark.parametrize("kind", ["foreign", "duplicate", "malformed", "mismatch", "conflict"])
def test_import_failure_preserves_all_sources(api_key_client, actor, project, create_user, kind):
    page = legacy_page(project, create_user, actor)
    if kind == "duplicate":
        legacy_page(project, create_user, actor)
    elif kind == "malformed":
        page.description_html = '<pre><code>{"kind":"agent-platform-hcom-team",broken}</code></pre>'
        page.save()
    elif kind == "mismatch":
        page.description_html = page.description_html.replace(str(project.id), str(uuid4()))
        page.save()
    elif kind == "foreign":
        page.description_html = page.description_html.replace(str(actor.id), str(uuid4()))
        page.save()
    elif kind == "conflict":
        ProjectTeamContact.objects.create(project=project, registration=actor, address="agent", target={}, active=False)
    source = page.description_html
    result = api_key_client.post(url(project, "import/"), {}, format="json")
    assert result.status_code == 409, result.data
    assert not ProjectTeamMigration.objects.exists()
    assert ProjectTeamContact.objects.count() == (1 if kind == "conflict" else 0)
    page.refresh_from_db()
    assert page.description_html == source and not page.is_locked


@pytest.mark.parametrize("api", [True, False])
@pytest.mark.parametrize("action", ["description", "metadata", "unlock", "delete"])
def test_imported_page_cannot_be_overwritten_or_unlocked(
    api_key_client, session_client, actor, project, create_user, api, action
):
    page = legacy_page(project, create_user, actor)
    assert api_key_client.post(url(project, "import/"), {}, format="json").status_code == 200
    client = api_key_client if api else session_client
    base = f"/api/{'v1/' if api else ''}workspaces/{project.workspace.slug}/projects/{project.id}/pages/{page.id}/"
    if action == "description":
        result = client.patch(base + "description/", {"description_html": "<p>overwrite</p>"}, format="json")
    elif action == "metadata":
        result = client.patch(base, {"name": "overwrite"}, format="json")
    elif action == "unlock":
        result = client.delete(base + "lock/")
    else:
        result = client.delete(base)
    assert result.status_code == 409, (base, action, result.status_code, getattr(result, "data", None))


def test_agent_create_and_member_management(api_key_client, actor, project):
    user = User.objects.create(username=uuid4().hex, email="member@test.invalid", display_name="New member")
    WorkspaceMember.objects.create(workspace=project.workspace, member=user, role=15)
    member_url = url(project, f"members/{user.id}/")
    assert api_key_client.post(member_url, {"role": 15}, format="json").status_code == 201
    payload = {
        "runtime": "hermes",
        "agent_key": "review",
        "display_name": "Reviewer",
        "role": "Review changes",
        "user_id": str(user.id),
        "binding": {"binding_id": str(uuid4()), "agent_id": "review", "profile": "project", "host_id": "local"},
    }
    response = api_key_client.post(url(project, "agents/"), payload, format="json", HTTP_IF_MATCH='"0"')
    assert response.status_code == 201, response.data
    identity = ProjectAgentRegistration.objects.get(id=response.data["id"])
    assert identity.api_token_id is None and identity.user_id == user.id
    assert api_key_client.post(url(project, "agents/"), payload, format="json", HTTP_IF_MATCH='"0"').status_code == 412
    assert contact(api_key_client, project, identity).status_code == 201
    assert api_key_client.patch(member_url, {"role": 5}, format="json").status_code == 200
    assert api_key_client.delete(member_url).status_code == 200
    assert api_key_client.post(url(project, "resolve/"), {"address": "agent"}, format="json").status_code == 404
    assert ProjectMember.objects.get(project=project, member=user).is_active is False
    assert User.objects.filter(id=user.id).exists()


def test_search_and_shared_identity_no_duplicate_users(api_key_client, actor, project):
    thread = str(uuid4())
    chat = ProjectAgentRegistration.objects.create(
        project=project,
        runtime="codex",
        agent_key="chat-" + thread.replace("-", ""),
        user=actor.user,
        display_name="Dedicated reviewer",
        provisioning={"role": "Review", "identity_key": "project"},
        binding={"thread_id": thread},
    )
    assert contact(api_key_client, project, chat, address="reviewer").status_code == 201
    result = api_key_client.get(url(project) + "?search=reviewer")
    assert len(result.data["actors"]) == len(result.data["contacts"]) == 1
    assert result.data["actors"][0]["identity_key"] == "project"
    assert ProjectMember.objects.filter(project=project).count() == 1


def test_import_rolls_back_valid_prefix_and_live_callback_is_blocked(
    api_key_client, actor, project, create_user, monkeypatch
):
    page = legacy_page(project, create_user, actor)
    document, _ = parse_document(page.description_html, project)
    document["contacts"]["broken"] = {
        **deepcopy(document["contacts"]["agent"]),
        "address": "broken",
        "registration_id": str(uuid4()),
    }
    page.description_html = "<pre><code>" + html.escape(json.dumps(document)) + "</code></pre>"
    page.save()
    assert api_key_client.post(url(project, "import/"), {}, format="json").status_code == 409
    assert not ProjectTeamContact.objects.filter(project=project).exists()
    del document["contacts"]["broken"]
    page.description_html = "<pre><code>" + html.escape(json.dumps(document)) + "</code></pre>"
    page.save()
    assert api_key_client.post(url(project, "import/"), {}, format="json").status_code == 200
    monkeypatch.setenv("LIVE_SERVER_SECRET_KEY", "test-live-key")
    path = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.id}/pages/{page.id}/description/"
    result = api_key_client.patch(
        path,
        {"description_html": "<p>replace</p>", "description_json": {}, "description_binary": ""},
        format="json",
        HTTP_LIVE_SERVER_SECRET_KEY="test-live-key",
    )
    assert result.status_code == 409
    duplicate = api_key_client.post(
        path.split(str(page.id))[0], {"name": "hcom team", "description_html": "<p>new</p>"}, format="json"
    )
    assert duplicate.status_code == 409


@pytest.mark.django_db(transaction=True)
def test_concurrent_contact_creation_has_one_winner(api_token, actor, project):
    barrier = Barrier(2)

    def publish():
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            barrier.wait(timeout=10)
            return contact(client, project, actor).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(lambda _: publish(), range(2)))
    assert sorted(statuses) == [201, 412]
    assert ProjectTeamContact.objects.filter(project=project).count() == 1


def test_cross_origin_precondition_and_etag_headers(api_key_client, project, settings):
    settings.CORS_ALLOWED_ORIGINS = ["http://127.0.0.1:18120"]
    response = api_key_client.options(
        url(project, "contacts/agent/", api=False),
        HTTP_ORIGIN="http://127.0.0.1:18120",
        HTTP_ACCESS_CONTROL_REQUEST_METHOD="PUT",
        HTTP_ACCESS_CONTROL_REQUEST_HEADERS="content-type,if-match",
    )
    assert response.status_code == 200
    assert "if-match" in response["Access-Control-Allow-Headers"].lower()
    response = api_key_client.get(url(project), HTTP_ORIGIN="http://127.0.0.1:18120")
    assert "etag" in response["Access-Control-Expose-Headers"].lower()


@pytest.mark.parametrize("operation", ["archive", "delete"])
def test_imported_descendant_prevents_ancestor_removal(api_key_client, actor, project, create_user, operation):
    page = legacy_page(project, create_user, actor)
    parent = Page.objects.create(workspace=project.workspace, owned_by=create_user, name="Parent")
    ProjectPage.objects.create(workspace=project.workspace, project=project, page=parent)
    page.parent = parent
    page.save()
    assert api_key_client.post(url(project, "import/"), {}, format="json").status_code == 200
    path = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.id}/pages/{parent.id}/"
    response = api_key_client.post(path + "archive/") if operation == "archive" else api_key_client.delete(path)
    assert response.status_code == 409, response.data
    page.refresh_from_db()
    assert page.archived_at is None
    assert api_key_client.get(path.replace(str(parent.id), str(page.id)) + "description/").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_session_edit_paused_before_row_lock_cannot_overwrite_import(
    api_token, actor, project, create_user, monkeypatch
):
    from plane.app.views.page.base import PagesDescriptionViewSet

    arrived, proceed = Event(), Event()
    original = PagesDescriptionViewSet.initial

    def paused_initial(self, request, *args, **kwargs):
        original(self, request, *args, **kwargs)
        if request.method == "PATCH":
            arrived.set()
            assert proceed.wait(timeout=15)

    monkeypatch.setattr(PagesDescriptionViewSet, "initial", paused_initial)
    page = legacy_page(project, create_user, actor)
    source = page.description_html
    path = f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/pages/{page.id}/description/"

    def edit():
        client = APIClient()
        client.force_authenticate(user=create_user)
        try:
            return client.patch(path, {"description_html": "<p>stale overwrite</p>"}, format="json").status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(edit)
        assert arrived.wait(timeout=15)
        importer = APIClient()
        importer.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            assert importer.post(url(project, "import/"), {}, format="json").status_code == 200
        finally:
            proceed.set()
        assert future.result(timeout=15) == 409
    page.refresh_from_db()
    assert page.description_html == source and page.is_locked


def test_new_agent_receiver_must_be_addressable(api_key_client, actor, project):
    payload = {
        "runtime": "codex",
        "agent_key": "invalid",
        "display_name": "Invalid",
        "role": "Review",
        "user_id": str(actor.user_id),
        "binding": {"binding_id": str(uuid4()), "thread_id": "not-a-uuid"},
    }
    response = api_key_client.post(url(project, "agents/"), payload, format="json", HTTP_IF_MATCH='"0"')
    assert response.status_code == 400, response.data
    assert not ProjectAgentRegistration.objects.filter(project=project, agent_key="invalid").exists()


def test_roster_query_count_does_not_grow_per_agent(api_key_client, actor, project):
    assert contact(api_key_client, project, actor).status_code == 201
    with CaptureQueriesContext(connection) as baseline:
        assert api_key_client.get(url(project)).status_code == 200
    for index in range(20):
        added = ProjectAgentRegistration.objects.create(
            project=project,
            runtime="codex",
            agent_key=f"agent-{index}",
            display_name=f"Agent {index}",
            user=actor.user,
            binding=actor.binding,
        )
        ProjectTeamContact.objects.create(
            project=project,
            registration=added,
            address=f"agent-{index}",
            target={k: v for k, v in actor.binding.items() if k != "binding_id"},
        )
    with CaptureQueriesContext(connection) as measured:
        response = api_key_client.get(url(project))
    assert response.status_code == 200 and len(response.data["contacts"]) == 21
    assert len(measured) <= len(baseline) + 1, (len(baseline), len(measured))


@pytest.mark.django_db(transaction=True)
def test_import_rechecks_discovery_after_waiting_for_page_mutation(api_token, actor, project, create_user, monkeypatch):
    from django.db.models.query import QuerySet

    page = legacy_page(project, create_user, actor)
    arrived, proceed = Event(), Event()
    original = QuerySet.select_for_update

    def paused_lock(queryset, *args, **kwargs):
        if queryset.model is Page and not arrived.is_set():
            arrived.set()
            assert proceed.wait(timeout=15)
        return original(queryset, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "select_for_update", paused_lock)

    def import_page():
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            return client.post(url(project, "import/"), {}, format="json").status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(import_page)
        assert arrived.wait(timeout=15)
        editor = APIClient()
        editor.force_authenticate(user=create_user)
        path = f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/pages/{page.id}/archive/"
        try:
            assert editor.post(path).status_code == 200
        finally:
            proceed.set()
        assert future.result(timeout=15) == 409
    assert not ProjectTeamMigration.objects.filter(project=project).exists()
    assert not ProjectTeamContact.objects.filter(project=project).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("operation", ["create", "rename"])
def test_page_admitted_before_import_cannot_create_duplicate_legacy_name(
    api_token, actor, project, create_user, monkeypatch, operation
):
    from plane.app.views.page.base import PageViewSet

    legacy_page(project, create_user, actor)
    other = Page.objects.create(workspace=project.workspace, owned_by=create_user, name="Other")
    ProjectPage.objects.create(workspace=project.workspace, project=project, page=other)
    arrived, proceed = Event(), Event()
    original = PageViewSet.initial

    def paused_initial(self, request, *args, **kwargs):
        original(self, request, *args, **kwargs)
        if request.data.get("name") == "hcom team":
            arrived.set()
            assert proceed.wait(timeout=15)

    monkeypatch.setattr(PageViewSet, "initial", paused_initial)

    def mutate():
        client = APIClient()
        client.force_authenticate(user=create_user)
        base = f"/api/workspaces/{project.workspace.slug}/projects/{project.id}/pages/"
        try:
            response = (
                client.post(base, {"name": "hcom team"}, format="json")
                if operation == "create"
                else client.patch(base + f"{other.id}/", {"name": "hcom team"}, format="json")
            )
            return response.status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(mutate)
        assert arrived.wait(timeout=15)
        importer = APIClient()
        importer.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            assert importer.post(url(project, "import/"), {}, format="json").status_code == 200
        finally:
            proceed.set()
        assert future.result(timeout=15) == 409
    assert Page.objects.filter(projects=project, name="hcom team", archived_at__isnull=True).count() == 1


def test_ignored_archive_body_does_not_acquire_project_lock_after_page(
    api_key_client, project, create_user, monkeypatch
):
    page = Page.objects.create(workspace=project.workspace, owned_by=create_user, name="Ordinary page")
    ProjectPage.objects.create(workspace=project.workspace, project=project, page=page)
    guard = Mock(side_effect=AssertionError("Archive must not acquire the name-mutation project lock."))
    monkeypatch.setattr("plane.utils.team.lock_legacy_page_name", guard)
    path = f"/api/v1/workspaces/{project.workspace.slug}/projects/{project.id}/pages/{page.id}/archive/"
    response = api_key_client.post(path, {"name": "hcom team"}, format="json")
    assert response.status_code == 200, response.data
    guard.assert_not_called()
