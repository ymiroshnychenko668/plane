"""Infrastructure v1 against real PostgreSQL, own identities and native routes.

All inventory, users, API tokens and hostnames below are disposable test fixtures.
No fixture contacts infrastructure or reads credential stores.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.db import connections
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import APIToken, Project, ProjectMember, User, Workspace, WorkspaceMember


pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    return make_project(workspace, create_user, "Owner")


@pytest.fixture
def consumer_project(workspace, create_user):
    return make_project(workspace, create_user, "Consumer")


@pytest.fixture
def payload():
    observed = (timezone.now() - timedelta(minutes=5)).isoformat()
    return {
        "key": "monitoring/grafana",
        "name": "Grafana",
        "kind": "service",
        "lifecycle": "active",
        "description": "Synthetic monitoring inventory",
        "tags": ["monitoring", "metrics"],
        "owner": {
            "name": "Operations",
            "reference": "team:operations",
            "recovery_contact": "role:supervisor",
        },
        "source": source(),
        "evidence": [{"kind": "review", "reference": "doc:monitoring-review", "observed_at": observed}],
        "last_verified": observed,
        "access_methods": [
            {
                "id": "grafana-api",
                "kind": "https",
                "endpoint": "https://grafana.example.invalid",
                "capability": "grafana",
                "operations": ["dashboards.read", "datasources.read"],
                "scope": "monitoring organization",
                "identity": "runtime-own-identity",
                "credential_refs": [
                    {
                        "provider": "openbao",
                        "path": "projects/example/monitoring/grafana",
                        "field": "api_token",
                        "purpose": "Read monitoring metadata",
                    }
                ],
                "notes": "Use assigned runtime credentials",
            }
        ],
    }


def source():
    return {"kind": "manual", "reference": "doc:monitoring-layout", "revision": "v1"}


def make_project(workspace, user, name, role=20):
    result = Project.objects.create(workspace=workspace, name=name, identifier=name[:5].upper())
    ProjectMember.objects.create(workspace=workspace, project=result, member=user, role=role)
    return result


def make_actor(workspace, project, role=15, workspace_role=15):
    suffix = uuid4().hex
    user = User.objects.create(
        email=f"infrastructure-{suffix}@example.invalid",
        username=f"infra-{suffix}",
        first_name="Inventory actor",
    )
    WorkspaceMember.objects.create(workspace=workspace, member=user, role=workspace_role)
    membership = ProjectMember.objects.create(workspace=workspace, project=project, member=user, role=role)
    token = APIToken.objects.create(user=user, workspace=workspace, token=f"synthetic-infrastructure-{suffix}")
    client = APIClient()
    client.credentials(HTTP_X_API_KEY=token.token)
    return user, membership, token, client


def url(workspace, project, suffix="resources/"):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/infrastructure/{suffix}"


def detail(workspace, project, resource):
    return url(workspace, project, f"resources/{resource['id']}/")


def create(client, workspace, project, payload, **overrides):
    data = {**deepcopy(payload), **overrides}
    return client.post(url(workspace, project), data, format="json", HTTP_IF_MATCH='"0"')


def created(client, workspace, project, payload, **overrides):
    response = create(client, workspace, project, payload, **overrides)
    assert response.status_code == 201, response.data
    return response.data


def relation(client, workspace, project, first, second, kind="depends_on"):
    return client.post(
        url(workspace, project, "relations/"),
        {"source_id": first["id"], "target_id": second["id"], "kind": kind, "source": source()},
        format="json",
        HTTP_IF_MATCH='"0"',
    )


def attach(client, workspace, project, resource, target, revision=None):
    return client.post(
        detail(workspace, project, resource) + "project-links/",
        {"project_id": str(target.id)},
        format="json",
        HTTP_IF_MATCH=f'"{revision if revision is not None else resource["revision"]}"',
    )


def error(response, status, code=None):
    assert response.status_code == status, response.data
    if code is not None:
        assert response.data["error"]["code"] == code, response.data


def test_crud_session_readback_discovery_and_stable_identity(api_key_client, create_user, workspace, project, payload):
    first = created(api_key_client, workspace, project, payload)
    assert first["revision"] == 1 and first["can_manage"] is True and first["can_detach"] is False
    assert first["ownership_project_id"] == str(project.id)
    path = detail(workspace, project, first)
    response = api_key_client.get(path)
    assert response.status_code == 200 and response["ETag"] == '"1"'
    assert response["Cache-Control"] == "no-store"
    assert response.data["access_methods"][0]["credential_refs"] == payload["access_methods"][0]["credential_refs"]
    page = api_key_client.get(url(workspace, project), {"key": payload["key"]}).data
    assert set(page) == {"results", "next_cursor", "has_more", "per_page"}
    assert [row["id"] for row in page["results"]] == [first["id"]]
    assert page["next_cursor"] is None and page["has_more"] is False
    native = APIClient()
    native.force_login(create_user)
    native_path = path.replace("/api/v1/", "/api/")
    assert native.get(native_path).data["id"] == first["id"]
    changed = native.patch(native_path, {"name": "Grafana dashboards"}, format="json", HTTP_IF_MATCH='"1"')
    assert changed.status_code == 200 and changed["ETag"] == '"2"'
    assert changed.data["source"] == first["source"] and changed.data["access_methods"] == first["access_methods"]
    assert api_key_client.get(path).data["name"] == "Grafana dashboards"
    capability = api_key_client.get(f"/api/v1/workspaces/{workspace.slug}/management-capabilities/").data
    assert "infrastructure" in capability["capabilities"]
    assert capability["infrastructure"]["version"] == 1
    assert capability["infrastructure"]["secret_values"] is False
    assert capability["infrastructure"]["pagination"]["max_per_page"] == 100
    assert native.delete(native_path, HTTP_IF_MATCH='"2"').status_code == 204
    error(api_key_client.get(path), 404, "not_found")
    assert api_key_client.get(url(workspace, project), {"key": payload["key"]}).data["results"] == []
    error(create(api_key_client, workspace, project, payload), 409, "conflict")


def test_create_defaults_and_noop_patch(api_key_client, workspace, project, payload):
    minimal = {key: payload[key] for key in ("key", "name", "kind", "owner", "source")}
    row = created(api_key_client, workspace, project, minimal)
    assert row["lifecycle"] == "planned" and row["description"] == ""
    assert row["tags"] == row["evidence"] == row["access_methods"] == []
    assert row["last_verified"] is None
    response = api_key_client.patch(detail(workspace, project, row), minimal, format="json", HTTP_IF_MATCH='W/"1"')
    assert response.status_code == 200 and response.data["revision"] == 1


@pytest.mark.parametrize("header", ["*", '"1", "2"', '"abc"', '"-1"', '"1.0"'])
def test_invalid_etag_is_not_a_blind_write(api_key_client, workspace, project, payload, header):
    row = created(api_key_client, workspace, project, payload)
    response = api_key_client.patch(
        detail(workspace, project, row), {"name": "Changed"}, format="json", HTTP_IF_MATCH=header
    )
    error(response, 400, "validation_error")
    assert api_key_client.get(detail(workspace, project, row)).data["revision"] == 1


def test_required_etags_and_unknown_outcome_reconciliation(api_key_client, workspace, project, payload):
    error(api_key_client.post(url(workspace, project), payload, format="json"), 428, "precondition_required")
    row = created(api_key_client, workspace, project, payload)
    error(create(api_key_client, workspace, project, payload), 409, "conflict")
    path = detail(workspace, project, row)
    error(api_key_client.patch(path, {"name": "Changed"}, format="json"), 428, "precondition_required")
    error(api_key_client.delete(path), 428, "precondition_required")
    response = api_key_client.patch(path, {"description": "Reviewed"}, format="json", HTTP_IF_MATCH='"1"')
    assert response.status_code == 200 and response["ETag"] == '"2"'
    error(api_key_client.patch(path, {"name": "Stale"}, format="json", HTTP_IF_MATCH='"1"'), 412, "revision_conflict")
    error(api_key_client.delete(path, HTTP_IF_MATCH='"1"'), 412, "revision_conflict")
    recovered = api_key_client.get(url(workspace, project), {"key": payload["key"]}).data["results"]
    assert len(recovered) == 1 and recovered[0]["description"] == "Reviewed"
    assert recovered[0]["id"] == row["id"] and recovered[0]["name"] == row["name"]


def test_native_session_cannot_replace_api_key(create_user, workspace, project, payload):
    client = APIClient()
    client.force_login(create_user)
    assert client.get(url(workspace, project)).status_code in (401, 403)
    assert create(client, workspace, project, payload).status_code in (401, 403)
    assert APIClient().get(url(workspace, project)).status_code in (401, 403)


@pytest.mark.parametrize("invalid_id", [1, True])
def test_relation_and_link_ids_require_uuid_strings(api_key_client, workspace, project, payload, invalid_id):
    row = created(api_key_client, workspace, project, payload)
    for field in ("source_id", "target_id"):
        body = {"source_id": row["id"], "target_id": row["id"], "kind": "depends_on", "source": source()}
        body[field] = invalid_id
        response = api_key_client.post(url(workspace, project, "relations/"), body, format="json", HTTP_IF_MATCH='"0"')
        error(response, 400, "validation_error")
    response = api_key_client.post(
        detail(workspace, project, row) + "project-links/",
        {"project_id": invalid_id},
        format="json",
        HTTP_IF_MATCH='"1"',
    )
    error(response, 400, "validation_error")


def test_key_workspace_and_actual_identity_not_role_headers(
    api_key_client, workspace, project, consumer_project, payload
):
    row = created(api_key_client, workspace, project, payload)
    user, _, token, client = make_actor(workspace, consumer_project, role=20, workspace_role=20)
    error(client.get(url(workspace, project)), 403)
    error(client.get(detail(workspace, consumer_project, row)), 404, "not_found")
    response = client.patch(
        detail(workspace, project, row),
        {"name": "Impersonated"},
        format="json",
        HTTP_IF_MATCH='"1"',
        HTTP_X_ROLE="admin",
        HTTP_X_USER_ID=str(workspace.owner_id),
    )
    error(response, 403)
    assert api_key_client.get(detail(workspace, project, row)).data["revision"] == 1
    other = Workspace.objects.create(name="Other", slug="other-infrastructure", owner=user)
    token.workspace = other
    token.save()
    error(client.get(url(workspace, consumer_project)), 403)


@pytest.mark.parametrize("inactive", ["workspace", "project"])
def test_inactive_membership_is_denied(api_key_client, workspace, project, payload, inactive):
    row = created(api_key_client, workspace, project, payload)
    user, member, _, client = make_actor(workspace, project)
    if inactive == "workspace":
        WorkspaceMember.objects.filter(workspace=workspace, member=user).update(is_active=False)
    else:
        member.is_active = False
        member.save()
    error(client.get(detail(workspace, project, row)), 403)
    error(client.get(url(workspace, project)), 403)


@pytest.mark.parametrize("role", [5, 15])
def test_guests_and_members_do_not_mutate(api_key_client, workspace, project, payload, role):
    row = created(api_key_client, workspace, project, payload)
    _, _, _, client = make_actor(workspace, project, role=role)
    if role == 5:
        project.guest_view_all_features = False
        project.save()
        error(client.get(url(workspace, project)), 403)
        project.guest_view_all_features = True
        project.save()
    read = client.get(detail(workspace, project, row))
    assert read.status_code == 200 and read.data["can_manage"] is False
    error(create(client, workspace, project, payload, key="unallowed"), 403)
    error(client.patch(detail(workspace, project, row), {"name": "No"}, format="json", HTTP_IF_MATCH='"1"'), 403)
    error(client.delete(detail(workspace, project, row), HTTP_IF_MATCH='"1"'), 403)


def test_shared_resource_identity_visibility_and_consumer_detach(
    api_key_client, workspace, project, consumer_project, payload
):
    grafana = created(api_key_client, workspace, project, payload)
    vm = created(api_key_client, workspace, project, payload, key="monitoring/vm", name="Monitoring VM", kind="vm")
    assert relation(api_key_client, workspace, project, grafana, vm, "runs_on").status_code == 201
    for row in (grafana, vm):
        response = attach(api_key_client, workspace, project, row, consumer_project)
        assert response.status_code == 200 and response.data["revision"] == 2
    _, _, _, consumer = make_actor(workspace, consumer_project, role=20, workspace_role=20)
    view = consumer.get(detail(workspace, consumer_project, grafana)).data
    assert view["id"] == grafana["id"] and view["ownership_project_id"] is None
    assert view["can_manage"] is False and view["can_detach"] is True
    links = consumer.get(detail(workspace, consumer_project, grafana) + "project-links/").data
    assert len(links["results"]) == 1
    assert links["results"][0]["project_id"] == str(consumer_project.id)
    assert links["results"][0]["kind"] == "serves_project" and links["results"][0]["is_owner"] is False
    assert str(project.id) not in str(view) and str(project.id) not in str(links)
    assert consumer.get(url(workspace, consumer_project, "relations/")).data["results"] == []
    error(
        consumer.patch(
            detail(workspace, consumer_project, grafana), {"name": "No"}, format="json", HTTP_IF_MATCH='"2"'
        ),
        403,
    )
    error(consumer.delete(detail(workspace, consumer_project, grafana), HTTP_IF_MATCH='"2"'), 403)
    error(api_key_client.delete(detail(workspace, project, grafana), HTTP_IF_MATCH='"2"'), 409, "conflict")
    own_edge = relation(consumer, workspace, consumer_project, grafana, vm, "runs_on")
    assert own_edge.status_code == 201
    assert len(api_key_client.get(url(workspace, project, "relations/")).data["results"]) == 1
    response = consumer.delete(
        detail(workspace, consumer_project, grafana) + f"project-links/{consumer_project.id}/", HTTP_IF_MATCH='"2"'
    )
    assert response.status_code == 204 and response["ETag"] == '"3"'
    error(consumer.get(detail(workspace, consumer_project, grafana)), 404, "not_found")
    assert consumer.get(url(workspace, consumer_project, "relations/")).data["results"] == []
    assert api_key_client.get(detail(workspace, project, grafana)).data["revision"] == 3
    assert len(api_key_client.get(url(workspace, project, "relations/")).data["results"]) == 1


def test_attach_requires_both_admins_and_masks_inaccessible_target(
    api_key_client, workspace, project, consumer_project, create_user, payload
):
    row = created(api_key_client, workspace, project, payload)
    membership = ProjectMember.objects.get(project=consumer_project, member=create_user)
    membership.role = 15
    membership.save()
    error(attach(api_key_client, workspace, project, row, consumer_project), 403, "forbidden")
    membership.is_active = False
    membership.save()
    error(attach(api_key_client, workspace, project, row, consumer_project), 404, "not_found")
    membership.is_active = True
    membership.role = 20
    membership.save()
    response = attach(api_key_client, workspace, project, row, consumer_project)
    assert response.status_code == 200 and response.data["revision"] == 2
    error(attach(api_key_client, workspace, project, row, consumer_project), 412, "revision_conflict")
    assert attach(api_key_client, workspace, project, row, consumer_project, revision=2).data["revision"] == 2
    error(
        api_key_client.delete(detail(workspace, project, row) + f"project-links/{project.id}/", HTTP_IF_MATCH='"2"'),
        409,
        "conflict",
    )
    membership.role = 15
    membership.save()
    error(
        api_key_client.delete(
            detail(workspace, project, row) + f"project-links/{consumer_project.id}/", HTTP_IF_MATCH='"2"'
        ),
        403,
    )


def test_project_link_guest_restrictions_apply_to_masking(
    api_key_client, workspace, project, consumer_project, payload
):
    row = created(api_key_client, workspace, project, payload)
    assert attach(api_key_client, workspace, project, row, consumer_project).status_code == 200
    user, _, _, client = make_actor(workspace, consumer_project, role=15)
    ProjectMember.objects.create(workspace=workspace, project=project, member=user, role=5)
    project.guest_view_all_features = False
    project.save()
    path = detail(workspace, consumer_project, row)
    assert client.get(path).data["ownership_project_id"] is None
    assert len(client.get(path + "project-links/").data["results"]) == 1
    project.guest_view_all_features = True
    project.save()
    assert client.get(path).data["ownership_project_id"] == str(project.id)
    assert len(client.get(path + "project-links/").data["results"]) == 2


def test_private_key_collision_is_generic_and_does_not_broaden_lookup(
    api_key_client, workspace, project, consumer_project, payload
):
    owner_resource = created(api_key_client, workspace, project, payload)
    _, _, _, consumer = make_actor(workspace, consumer_project, role=20)
    assert consumer.get(url(workspace, consumer_project), {"key": payload["key"]}).data["results"] == []
    response = create(consumer, workspace, consumer_project, payload)
    error(response, 409, "conflict")
    assert owner_resource["id"] not in str(response.data) and str(project.id) not in str(response.data)
    error(consumer.get(detail(workspace, consumer_project, owner_resource)), 404, "not_found")


def test_monitoring_graph_and_relation_readback_delete(api_key_client, workspace, project):
    example = json.loads(
        (Path(__file__).resolve().parents[6] / "docs/infrastructure-monitoring-example-v1.json").read_text()
    )
    resources = {
        payload["key"]: created(api_key_client, workspace, project, payload) for payload in example["resources"]
    }
    edges = [(edge["source_key"], edge["target_key"], edge["kind"]) for edge in example["relations"]]
    rows = []
    for first, second, kind in edges:
        response = relation(api_key_client, workspace, project, resources[first], resources[second], kind)
        assert response.status_code == 201, response.data
        assert response["ETag"] == '"1"' and response.data["can_manage"] is True
        rows.append(response.data)
    assert len(api_key_client.get(url(workspace, project, "relations/")).data["results"]) == 6
    grafana = resources["monitoring/grafana"]
    filters = {"source_id": grafana["id"], "target_id": resources["monitoring/vm"]["id"], "kind": "runs_on"}
    readback = api_key_client.get(url(workspace, project, "relations/"), filters).data["results"]
    assert len(readback) == 1 and readback[0]["source"] == source()
    error(relation(api_key_client, workspace, project, grafana, resources["monitoring/vm"], "runs_on"), 409, "conflict")
    path = url(workspace, project, f"relations/{readback[0]['id']}/")
    error(api_key_client.delete(path), 428, "precondition_required")
    error(api_key_client.delete(path, HTTP_IF_MATCH='"2"'), 412, "revision_conflict")
    assert api_key_client.delete(path, HTTP_IF_MATCH='"1"').status_code == 204
    assert api_key_client.get(url(workspace, project, "relations/"), filters).data["results"] == []
    assert (
        api_key_client.delete(detail(workspace, project, resources["monitoring"]), HTTP_IF_MATCH='"1"').status_code
        == 204
    )
    assert len(api_key_client.get(url(workspace, project, "relations/")).data["results"]) == 3


def test_relation_invalid_endpoints_types_cycles_and_retired(
    api_key_client, workspace, project, consumer_project, payload
):
    service = created(api_key_client, workspace, project, payload)
    vm = created(api_key_client, workspace, project, payload, key="monitoring/vm", kind="vm")
    host = created(api_key_client, workspace, project, payload, key="lab/host", kind="physical_host")
    hidden = created(api_key_client, workspace, consumer_project, payload, key="consumer/hidden")
    error(relation(api_key_client, workspace, project, service, service), 400, "validation_error")
    error(relation(api_key_client, workspace, project, service, hidden), 404, "not_found")
    error(relation(api_key_client, workspace, project, host, vm, "runs_on"), 400, "validation_error")
    error(relation(api_key_client, workspace, project, vm, service, "runs_on"), 400, "validation_error")
    assert relation(api_key_client, workspace, project, service, vm).status_code == 201
    assert relation(api_key_client, workspace, project, vm, host).status_code == 201
    error(relation(api_key_client, workspace, project, host, service), 400, "validation_error")
    assert (
        api_key_client.patch(
            detail(workspace, project, host), {"lifecycle": "retired"}, format="json", HTTP_IF_MATCH='"1"'
        ).status_code
        == 200
    )
    error(relation(api_key_client, workspace, project, service, host, "runs_on"), 400, "validation_error")


@pytest.mark.parametrize(
    "field,value",
    [("key", "replacement"), ("kind", "vm"), ("revision", 2), ("can_manage", True), ("ownership_project_id", "unused")],
)
def test_identity_and_server_fields_are_immutable(api_key_client, workspace, project, payload, field, value):
    row = created(api_key_client, workspace, project, payload)
    response = api_key_client.patch(detail(workspace, project, row), {field: value}, format="json", HTTP_IF_MATCH='"1"')
    error(response, 400, "validation_error")
    assert api_key_client.get(detail(workspace, project, row)).data["revision"] == 1


@pytest.mark.parametrize(
    "invalid",
    [{"unexpected": "SYNTHETIC_REJECTED_VALUE"}, ["SYNTHETIC_REJECTED_VALUE"], "SYNTHETIC_REJECTED_VALUE"],
)
def test_top_level_invalid_bodies_are_safe_client_errors(api_key_client, workspace, project, payload, invalid):
    row = created(api_key_client, workspace, project, payload)
    requests = (
        ("post", url(workspace, project), '"0"'),
        ("patch", detail(workspace, project, row), '"1"'),
        ("post", url(workspace, project, "relations/"), '"0"'),
        ("post", detail(workspace, project, row) + "project-links/", '"1"'),
    )
    for method, path, revision in requests:
        response = getattr(api_key_client, method)(path, invalid, format="json", HTTP_IF_MATCH=revision)
        error(response, 400, "validation_error")
        assert "SYNTHETIC_REJECTED_VALUE" not in str(response.data)
    assert api_key_client.get(detail(workspace, project, row)).data["revision"] == 1


@pytest.mark.parametrize(
    "case",
    [
        "secret_value",
        "nested_header",
        "userinfo",
        "query",
        "fragment",
        "scheme",
        "secret_text",
        "bearer",
        "pem",
        "control",
        "path_traversal",
        "path_absolute",
        "provider",
        "duplicate_access",
        "duplicate_operation",
        "future",
        "naive",
        "no_evidence",
        "source_unknown",
        "provenance_userinfo",
        "encoded_credentials",
        "refresh_token",
    ],
)
def test_rejects_secret_shapes_and_invalid_evidence_atomically(api_key_client, workspace, project, payload, case):
    data = deepcopy(payload)
    sentinel = "SYNTHETIC_REJECTED_VALUE"
    access = data["access_methods"][0]
    if case == "secret_value":
        access["credential_refs"][0]["value"] = sentinel
    elif case == "nested_header":
        access["headers"] = {"Authorization": sentinel}
    elif case == "userinfo":
        access["endpoint"] = f"https://user:{sentinel}@grafana.example.invalid"
    elif case == "query":
        access["endpoint"] += f"?token={sentinel}"
    elif case == "fragment":
        access["endpoint"] += f"#{sentinel}"
    elif case == "scheme":
        access["endpoint"] = "ssh://grafana.example.invalid"
    elif case == "secret_text":
        data["description"] = f"password={sentinel}"
    elif case == "provenance_userinfo":
        data["source"]["reference"] = f"https://user:{sentinel}@evidence.example.invalid"
    elif case == "encoded_credentials":
        data["description"] = f"password%253D{sentinel}"
    elif case == "refresh_token":
        data["description"] = f"refresh_token={sentinel}"
    elif case == "bearer":
        data["owner"]["name"] = f"Bearer {sentinel}"
    elif case == "pem":
        data["description"] = f"-----BEGIN PRIVATE KEY-----\n{sentinel}\n-----END PRIVATE KEY-----"
    elif case == "control":
        data["description"] = f"text\x00{sentinel}"
    elif case == "path_traversal":
        access["credential_refs"][0]["path"] = "projects/../private"
    elif case == "path_absolute":
        access["credential_refs"][0]["path"] = "/projects/private"
    elif case == "provider":
        access["credential_refs"][0]["provider"] = "inline"
    elif case == "duplicate_access":
        data["access_methods"].append(deepcopy(access))
    elif case == "duplicate_operation":
        access["operations"].append(access["operations"][0])
    elif case == "future":
        data["last_verified"] = (timezone.now() + timedelta(days=1)).isoformat()
    elif case == "naive":
        data["evidence"][0]["observed_at"] = "2026-10-01T10:00:00"
    elif case == "no_evidence":
        data["evidence"] = []
    else:
        data["source"]["password"] = sentinel
    response = create(api_key_client, workspace, project, data)
    error(response, 400, "validation_error")
    assert sentinel not in str(response.data)
    assert api_key_client.get(url(workspace, project)).data["results"] == []


@pytest.mark.parametrize(
    "filters",
    [
        {"per_page": "0"},
        {"per_page": "101"},
        {"per_page": "1.5"},
        {"per_page": "9" * 100},
        {"kind": "pod"},
        {"lifecycle": "running"},
        {"unknown": "x"},
        {"cursor": "forged"},
        {"search": "x" * 201},
        {"tags": ",".join(f"tag{i}" for i in range(21))},
    ],
)
def test_invalid_resource_filters(api_key_client, workspace, project, filters):
    error(api_key_client.get(url(workspace, project), filters), 400)


def test_cursor_pagination_filtering_context_and_acl_recheck(
    api_key_client, workspace, project, consumer_project, payload
):
    for index in range(5):
        created(
            api_key_client,
            workspace,
            project,
            payload,
            key=f"monitoring/{index}",
            tags=["monitoring", "even"] if index % 2 == 0 else ["monitoring"],
        )
    query = {"per_page": 2, "kind": "service", "tags": "monitoring", "search": "grafana", "lifecycle": "active"}
    first = api_key_client.get(url(workspace, project), query)
    assert first.status_code == 200 and len(first.data["results"]) == 2
    assert first.data["has_more"] is True and first.data["next_cursor"]
    cursor = first.data["next_cursor"]
    all_rows = first.data["results"][:]
    while cursor:
        response = api_key_client.get(url(workspace, project), {**query, "cursor": cursor})
        assert response.status_code == 200 and len(response.data["results"]) <= 2
        all_rows += response.data["results"]
        cursor = response.data["next_cursor"]
    ids = [row["id"] for row in all_rows]
    assert len(ids) == len(set(ids)) == 5 and ids == sorted(ids)
    even = api_key_client.get(url(workspace, project), {"tags": "monitoring,even"}).data["results"]
    assert len(even) == 3
    cursor = first.data["next_cursor"]
    for filters in ({**query, "cursor": cursor, "per_page": 3}, {**query, "cursor": cursor, "search": "other"}):
        error(api_key_client.get(url(workspace, project), filters), 400, "invalid_cursor")
    error(api_key_client.get(url(workspace, consumer_project), {**query, "cursor": cursor}), 400, "invalid_cursor")
    _, membership, _, client = make_actor(workspace, project)
    error(client.get(url(workspace, project), {**query, "cursor": cursor}), 400, "invalid_cursor")
    own_cursor = client.get(url(workspace, project), query).data["next_cursor"]
    membership.is_active = False
    membership.save()
    error(client.get(url(workspace, project), {**query, "cursor": own_cursor}), 403)
    error(api_key_client.get(url(workspace, project) + "?kind=service&kind=vm"), 400, "validation_error")


@pytest.mark.parametrize(
    "filters",
    [
        {"source_id": "bad"},
        {"target_id": "bad"},
        {"resource_id": "bad"},
        {"kind": "serves_project"},
        {"key": "unknown"},
    ],
)
def test_invalid_relation_filters(api_key_client, workspace, project, filters):
    error(api_key_client.get(url(workspace, project, "relations/"), filters), 400, "validation_error")


def test_relations_and_project_links_use_bounded_pages(
    api_key_client, workspace, project, consumer_project, create_user, payload
):
    rows = [created(api_key_client, workspace, project, payload, key=f"graph/{index}") for index in range(4)]
    for target in rows[1:]:
        assert relation(api_key_client, workspace, project, rows[0], target).status_code == 201
    query = {"resource_id": rows[0]["id"], "per_page": 2}
    first = api_key_client.get(url(workspace, project, "relations/"), query).data
    assert len(first["results"]) == 2 and first["has_more"] is True
    second = api_key_client.get(url(workspace, project, "relations/"), {**query, "cursor": first["next_cursor"]}).data
    assert len(second["results"]) == 1 and second["has_more"] is False
    ids = [item["id"] for item in first["results"] + second["results"]]
    assert ids == sorted(ids) and len(set(ids)) == 3
    _, _, _, reader = make_actor(workspace, project, role=15)
    assert all(
        item["can_manage"] is False for item in reader.get(url(workspace, project, "relations/")).data["results"]
    )
    error(relation(reader, workspace, project, rows[1], rows[2]), 403)
    error(
        reader.delete(url(workspace, project, f"relations/{ids[0]}/"), HTTP_IF_MATCH='"1"'),
        403,
    )
    assert attach(api_key_client, workspace, project, rows[0], consumer_project).status_code == 200
    third_project = make_project(workspace, create_user, "Third")
    assert attach(api_key_client, workspace, project, rows[0], third_project, revision=2).status_code == 200
    path = detail(workspace, project, rows[0]) + "project-links/"
    first_link = api_key_client.get(path, {"per_page": 2}).data
    assert len(first_link["results"]) == 2 and first_link["has_more"] is True
    second_link = api_key_client.get(path, {"per_page": 2, "cursor": first_link["next_cursor"]}).data
    assert len(second_link["results"]) == 1 and second_link["has_more"] is False
    assert {item["revision"] for item in first_link["results"] + second_link["results"]} == {3}
    error(api_key_client.get(path, {"per_page": 101}), 400, "validation_error")
    error(api_key_client.get(path, {"kind": "serves_project"}), 400, "validation_error")


def test_cross_workspace_resource_and_link_references_are_hidden(
    api_key_client, workspace, project, create_user, payload
):
    row = created(api_key_client, workspace, project, payload)
    other_workspace = Workspace.objects.create(name="Foreign", slug="foreign-infrastructure", owner=create_user)
    WorkspaceMember.objects.create(workspace=other_workspace, member=create_user, role=20)
    other_project = make_project(other_workspace, create_user, "Foreign")
    foreign = created(api_key_client, other_workspace, other_project, payload)
    error(api_key_client.get(detail(workspace, project, foreign)), 404, "not_found")
    error(relation(api_key_client, workspace, project, row, foreign), 404, "not_found")
    error(attach(api_key_client, workspace, project, row, other_project), 404, "not_found")
    assert api_key_client.get(detail(workspace, project, row)).data["revision"] == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_updates_only_one_writer_wins(api_key_client, api_token, workspace, project, payload):
    row = created(api_key_client, workspace, project, payload)
    barrier = Barrier(2)

    def update(name):
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            barrier.wait(timeout=10)
            return client.patch(
                detail(workspace, project, row), {"name": name}, format="json", HTTP_IF_MATCH='"1"'
            ).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(update, ["First observation", "Second observation"]))
    assert sorted(outcomes) == [200, 412]
    final = api_key_client.get(detail(workspace, project, row)).data
    assert final["revision"] == 2 and final["name"] in {"First observation", "Second observation"}


@pytest.mark.django_db(transaction=True)
def test_concurrent_creates_keep_one_stable_resource(api_key_client, api_token, workspace, project, payload):
    barrier = Barrier(2)

    def write(_):
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            barrier.wait(timeout=10)
            return create(client, workspace, project, payload).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, range(2)))
    assert sorted(outcomes) == [201, 409]
    assert len(api_key_client.get(url(workspace, project), {"key": payload["key"]}).data["results"]) == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_edges_cannot_create_cycle(api_key_client, api_token, workspace, project, payload):
    first = created(api_key_client, workspace, project, payload)
    second = created(api_key_client, workspace, project, payload, key="monitoring/prometheus")
    barrier = Barrier(2)

    def write(pair):
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            barrier.wait(timeout=10)
            return relation(client, workspace, project, *pair).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(write, [(first, second), (second, first)]))
    assert sorted(outcomes) == [201, 400]
    assert len(api_key_client.get(url(workspace, project, "relations/")).data["results"]) == 1
