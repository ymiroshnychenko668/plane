"""Build evidence contracts against PostgreSQL, real permissions and routes."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.db import connections
from django.utils import timezone
from rest_framework.test import APIClient

from plane.db.models import Build, BuildResult, Module, Project, ProjectMember, Workspace, WorkspaceMember

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Build evidence", identifier="BLD", module_view=True)
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20)
    return project


@pytest.fixture
def module(project):
    return Module.objects.create(project=project, name="Backend")


@pytest.fixture
def payload(module):
    now = timezone.now()
    metric = {
        "covered": 8,
        "total": 10,
        "revision": "a" * 40,
        "tool": "coverage.py",
        "measured_at": (now - timedelta(minutes=1)).isoformat(),
        "source": "https://github.com/acme/service/actions/runs/123/artifacts/1",
    }
    return {
        "repository": "acme/service",
        "workflow": "CI",
        "run_id": "123",
        "attempt": 1,
        "revision": "a" * 40,
        "ref": "refs/heads/main",
        "pipeline_url": "https://github.com/acme/service/actions/runs/123",
        "status": "completed",
        "conclusion": "success",
        "started_at": (now - timedelta(minutes=3)).isoformat(),
        "finished_at": (now - timedelta(minutes=1)).isoformat(),
        "observed_at": now.isoformat(),
        "freshness_hours": 24,
        "links": [{"title": "Coverage report", "url": "https://github.com/acme/service/actions/runs/123/artifacts/1"}],
        "results": [
            {
                "module_id": str(module.id),
                "suite": "api-unit",
                "test_type": "unit",
                "scope": "apps/api",
                "exclusions": ["generated/"],
                "tests": {"passed": 20, "failed": 0, "skipped": 2},
                "coverage": {"line": metric},
            }
        ],
    }


def url(workspace, project, suffix=""):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/builds/{suffix}"


def publish(client, workspace, project, data, version=0):
    return client.post(url(workspace, project, "publish/"), data, format="json", HTTP_IF_MATCH=f'"{version}"')


def test_publish_read_session_and_capability(api_key_client, session_client, workspace, project, payload):
    response = publish(api_key_client, workspace, project, payload)
    assert response.status_code == 201, response.data
    assert response["ETag"] == '"1"'
    build = response.data
    assert build["version"] == 1
    assert build["duration_seconds"] == 120
    metric = build["results"][0]["coverage"]["line"]
    assert metric["covered"] == 8 and metric["total"] == 10 and metric["percent"] == 80
    assert metric["evidence_states"] == ["measured"]
    assert api_key_client.get(url(workspace, project, f"{build['id']}/")).status_code == 200
    session_path = url(workspace, project, f"{build['id']}/").replace("/api/v1/", "/api/")
    assert session_client.get(session_path).status_code == 200
    assert session_client.post(
        session_path.replace(f"{build['id']}/", "publish/"), payload, format="json"
    ).status_code in (404, 405)
    capability = api_key_client.get(f"/api/v1/workspaces/{workspace.slug}/management-capabilities/").data
    assert "builds" in capability["capabilities"]


def test_idempotence_strict_cas_and_partial_preservation(api_key_client, workspace, project, payload):
    initial = publish(api_key_client, workspace, project, payload)
    assert publish(api_key_client, workspace, project, payload, version=1).data["version"] == 1
    assert publish(api_key_client, workspace, project, payload, version=0).status_code == 412
    assert api_key_client.post(url(workspace, project, "publish/"), payload, format="json").status_code == 428
    partial = {key: payload[key] for key in ("repository", "run_id", "attempt", "observed_at")}
    partial["results"] = [
        {"module_id": payload["results"][0]["module_id"], "suite": "api-unit", "tests": {"passed": 21}}
    ]
    updated = publish(api_key_client, workspace, project, partial, version=1)
    assert updated.status_code == 200, updated.data
    assert updated.data["version"] == 2
    assert updated.data["results"][0]["tests"] == {"passed": 21, "failed": 0, "skipped": 2}
    assert updated.data["results"][0]["coverage"] == initial.data["results"][0]["coverage"]
    partial.pop("results")
    assert publish(api_key_client, workspace, project, partial, version=2).data["version"] == 2


def test_metrics_suites_and_missing_data_are_independent(api_key_client, workspace, project, payload):
    payload["results"][0]["coverage"]["line"].update(covered=0, total=0)
    payload["results"].append(
        {"module_id": None, "suite": "whole-project", "test_type": "other", "scope": "all measured source"}
    )
    response = publish(api_key_client, workspace, project, payload)
    assert response.status_code == 201, response.data
    rows = {row["suite"]: row for row in response.data["results"]}
    assert rows["api-unit"]["coverage"]["line"]["percent"] is None
    assert "branch" not in rows["api-unit"]["coverage"]
    assert rows["whole-project"]["coverage"] == {}
    assert rows["whole-project"]["tests"] is None
    assert rows["whole-project"]["evidence_states"] == ["not_measured"]
    payload["results"] = [payload["results"][0]]
    payload["results"][0]["coverage"] = {"branch": deepcopy(payload["results"][0]["coverage"]["line"])}
    response = publish(api_key_client, workspace, project, payload, version=1)
    rows = {row["suite"]: row for row in response.data["results"]}
    assert set(rows["api-unit"]["coverage"]) == {"line", "branch"}
    assert "whole-project" in rows


def test_stale_and_revision_mismatch_preserve_measured_timestamp(api_key_client, workspace, project, payload):
    metric = payload["results"][0]["coverage"]["line"]
    measured_at = (timezone.now() - timedelta(days=2)).isoformat()
    metric.update(revision="b" * 40, measured_at=measured_at)
    response = publish(api_key_client, workspace, project, payload)
    assert response.status_code == 201, response.data
    metric = response.data["results"][0]["coverage"]["line"]
    assert set(metric["evidence_states"]) == {"stale", "revision_mismatch"}
    reread = api_key_client.get(url(workspace, project, f"{response.data['id']}/"))
    assert reread.data["results"][0]["coverage"]["line"]["measured_at"] == metric["measured_at"]


@pytest.mark.parametrize(
    "kind", ["negative", "boolean", "fraction", "above_total", "short_revision", "naive", "future", "unknown_metric"]
)
def test_rejects_invalid_measurements_atomically(api_key_client, workspace, project, payload, kind):
    metric = payload["results"][0]["coverage"]["line"]
    if kind == "negative":
        metric["covered"] = -1
    elif kind == "boolean":
        metric["covered"] = True
    elif kind == "fraction":
        metric["covered"] = 1.5
    elif kind == "above_total":
        metric["covered"] = 11
    elif kind == "short_revision":
        metric["revision"] = "abcd"
    elif kind == "naive":
        metric["measured_at"] = "2026-01-01T10:00:00"
    elif kind == "future":
        metric["measured_at"] = (timezone.now() + timedelta(days=1)).isoformat()
    else:
        payload["results"][0]["coverage"]["made_up"] = metric
    response = publish(api_key_client, workspace, project, payload)
    assert response.status_code == 400, response.data
    assert not Build.objects.exists() and not BuildResult.objects.exists()


def test_module_workspace_and_role_boundaries(api_key_client, api_token, create_user, workspace, project, payload):
    other = Project.objects.create(workspace=workspace, name="Other project", identifier="OTH")
    module = Module.objects.create(project=other, name="Private")
    payload["results"][0]["module_id"] = str(module.id)
    assert publish(api_key_client, workspace, project, payload).status_code == 400
    assert api_key_client.get(url(workspace, other)).status_code == 403
    membership = ProjectMember.objects.get(project=project, member=create_user)
    membership.role = 5
    membership.save()
    assert api_key_client.get(url(workspace, project)).status_code == 200
    assert publish(api_key_client, workspace, project, payload).status_code == 403
    elsewhere = Workspace.objects.create(owner=create_user, name="Elsewhere", slug="elsewhere")
    WorkspaceMember.objects.create(workspace=elsewhere, member=create_user, role=20)
    remote_project = Project.objects.create(workspace=elsewhere, name="Elsewhere project", identifier="ELSE")
    ProjectMember.objects.create(project=remote_project, member=create_user, role=20)
    api_token.workspace = workspace
    api_token.save()
    assert api_key_client.get(url(elsewhere, remote_project)).status_code == 403


@pytest.mark.parametrize("kind", ["older", "status", "revision", "workflow", "scope", "evidence", "conclusion"])
def test_rejects_regressions_without_modifying_record(api_key_client, workspace, project, payload, kind):
    initial = publish(api_key_client, workspace, project, payload)
    assert initial.status_code == 201
    if kind == "older":
        payload["observed_at"] = (timezone.now() - timedelta(hours=1)).isoformat()
    elif kind == "status":
        payload["status"] = "running"
        payload["conclusion"] = None
        payload["finished_at"] = None
    elif kind == "revision":
        payload["revision"] = "b" * 40
    elif kind == "workflow":
        payload["workflow"] = "Renamed"
    elif kind == "scope":
        payload["results"][0]["scope"] = "different code"
    elif kind == "conclusion":
        payload["conclusion"] = "failure"
    else:
        payload["results"][0]["coverage"]["line"]["measured_at"] = (timezone.now() - timedelta(hours=2)).isoformat()
    response = publish(api_key_client, workspace, project, payload, version=1)
    assert response.status_code == 409, response.data
    build = Build.objects.get()
    assert build.version == 1 and build.status == "completed"
    assert build.results.get().scope == "apps/api"


def test_rerun_attempt_is_a_separate_build(api_key_client, workspace, project, payload):
    first = publish(api_key_client, workspace, project, payload)
    payload["attempt"] = 2
    second = publish(api_key_client, workspace, project, payload)
    assert second.status_code == 201 and second.data["id"] != first.data["id"]
    assert Build.objects.count() == 2
    assert api_key_client.get(url(workspace, project), {"attempt": 2}).data["total_results"] == 1


def test_latest_summary_is_not_limited_by_history_page(api_key_client, workspace, project, module, payload):
    unmeasured = Module.objects.create(project=project, name="No reports yet")
    older = publish(api_key_client, workspace, project, payload).data
    base = Build.objects.get()
    for index in range(55):
        build = Build.objects.create(
            project=project,
            repository=base.repository,
            workflow=base.workflow,
            run_id=str(index + 200),
            attempt=1,
            revision=base.revision,
            ref="refs/heads/feature",
            pipeline_url=base.pipeline_url,
            status="completed",
            observed_at=base.observed_at + timedelta(minutes=index + 1),
        )
        BuildResult.objects.create(
            project=project, build=build, module=module, suite="no-coverage", test_type="unit", scope="apps/api"
        )
    response = api_key_client.get(url(workspace, project), {"per_page": 2})
    assert len(response.data["results"]) == 2 and response.data["next_page_results"] is True
    summary_url = url(workspace, project).replace("builds/", "builds-summary/")
    response = api_key_client.get(summary_url)
    assert response.status_code == 200, response.data
    modules = {row["module_id"]: row for row in response.data["modules"]}
    assert modules[str(module.id)]["latest_build"]["run_id"] == "254"
    assert modules[str(module.id)]["last_measured_build"]["id"] == older["id"]
    assert modules[str(unmeasured.id)]["latest_build"] is None
    assert response.data["project"]["latest_build"] is None
    main_only = api_key_client.get(summary_url, {"ref": "refs/heads/main"}).data
    assert (
        main_only["modules"][1 if main_only["modules"][0]["module_id"] == str(unmeasured.id) else 0]["latest_build"][
            "id"
        ]
        == older["id"]
    )


def test_missing_api_key_and_invalid_filters(api_client, api_key_client, workspace, project):
    assert APIClient().get(url(workspace, project)).status_code in (401, 403)
    for filters in (
        {"attempt": "0"},
        {"attempt": "9" * 5000},
        {"module_id": "bad"},
        {"status": "green"},
        {"per_page": "bad"},
        {"per_page": "0"},
    ):
        response = api_key_client.get(url(workspace, project), filters)
        assert response.status_code == 400, (filters, response.data)


def test_browser_session_does_not_replace_api_key(create_user, workspace, project, payload):
    client = APIClient()
    client.force_login(create_user)
    assert client.get(url(workspace, project)).status_code in (401, 403)
    assert publish(client, workspace, project, payload).status_code in (401, 403)
    assert not Build.objects.exists()


def test_bare_numeric_precondition_and_explicit_null_tests(api_key_client, workspace, project, payload):
    response = api_key_client.post(url(workspace, project, "publish/"), payload, format="json", HTTP_IF_MATCH="0")
    assert response.status_code == 201 and response["ETag"] == '"1"'
    payload["results"][0]["tests"] = None
    response = publish(api_key_client, workspace, project, payload, version=1)
    assert response.status_code == 200 and response.data["results"][0]["tests"] is None


@pytest.mark.django_db(transaction=True)
def test_concurrent_publish_only_one_writer_wins(api_token, workspace, project, payload):
    barrier = Barrier(2)

    def create():
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            barrier.wait(timeout=10)
            response = publish(client, workspace, project, deepcopy(payload))
            return response.status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))
    assert sorted(results) == [201, 412]
    assert Build.objects.count() == 1 and BuildResult.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_updates_keep_one_complete_observation(api_key_client, api_token, workspace, project, payload):
    assert publish(api_key_client, workspace, project, payload).status_code == 201
    barrier = Barrier(2)

    def update(count):
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        data = deepcopy(payload)
        data["results"][0]["tests"]["passed"] = count
        try:
            barrier.wait(timeout=10)
            return publish(client, workspace, project, data, version=1).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, [30, 40]))
    assert sorted(results) == [200, 412]
    build = Build.objects.get()
    assert build.version == 2 and build.results.get().tests["passed"] in {30, 40}
