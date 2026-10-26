# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Real ORM/API-key contracts; only live HTTP and asynchronous tasks are faked.

Editor codec and open-document behavior are tested by the live package. These
tests prove Django persistence, permission and revision behavior against Postgres.
"""

import base64
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.db import connections
from rest_framework.test import APIClient

from plane.api.views.management import page_revision
from plane.db.models import (
    FileAsset,
    Issue,
    IssueRelation,
    Page,
    PageVersion,
    Project,
    ProjectMember,
    ProjectPage,
    State,
    User,
    Workspace,
    WorkspaceMember,
)

pytestmark = [pytest.mark.contract, pytest.mark.django_db]


@pytest.fixture(autouse=True)
def local_dependencies(settings, monkeypatch):
    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    settings.LIVE_URL = "http://plane-live.test/live"
    settings.APP_BASE_URL = "http://plane-app.test"
    settings.WEB_URL = "http://plane-app.test"
    monkeypatch.setenv("LIVE_SERVER_SECRET_KEY", "test-live-secret")
    monkeypatch.setattr("celery.app.task.Task.delay", Mock())
    cache.clear()


@pytest.fixture
def project(workspace, create_user):
    project = Project.objects.create(workspace=workspace, name="Managed project", identifier="MNG")
    ProjectMember.objects.create(workspace=workspace, project=project, member=create_user, role=20)
    return project


@pytest.fixture
def page(workspace, project, create_user):
    page = Page.objects.create(
        workspace=workspace,
        owned_by=create_user,
        name="Build status",
        description_html="<p>Before</p>",
        description_json={"type": "doc"},
        description_binary=b"\x01\x02\x03\x04",
    )
    ProjectPage.objects.create(workspace=workspace, project=project, page=page)
    return page


def project_url(workspace, project):
    return f"/api/v1/workspaces/{workspace.slug}/projects/{project.id}/"


def page_url(workspace, project, page):
    return project_url(workspace, project) + f"pages/{page.id}/"


def member_url(workspace):
    return f"/api/v1/workspaces/{workspace.slug}/workspace-members/"


def test_api_key_required_and_session_not_substitute(api_client, create_user, workspace):
    url = f"/api/v1/workspaces/{workspace.slug}/management-capabilities/"
    assert api_client.get(url).status_code in (401, 403)
    api_client.force_login(create_user)
    assert api_client.get(url).status_code in (401, 403)
    api_client.credentials(HTTP_X_API_KEY="invalid-token")
    assert api_client.get(url).status_code in (401, 403)


def test_capabilities_and_workspace_discovery(api_key_client, workspace, create_user):
    other = User.objects.create(email="other@plane.test", username="other-discovery")
    Workspace.objects.create(name="Other workspace", slug="other-workspace", owner=other)
    response = api_key_client.get("/api/v1/workspaces/")
    assert response.status_code == 200, response.data
    data = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
    assert [item["slug"] for item in data] == [workspace.slug]
    response = api_key_client.get(f"/api/v1/workspaces/{workspace.slug}/management-capabilities/")
    assert response.status_code == 200
    assert response.data["version"] == 3
    assert {"pages", "work_item_relation_ids", "generic_asset_delete", "agent_registrations"}.issubset(
        response.data["capabilities"]
    )
    assert response.data["native_releases"] is False
    assert api_key_client.get("/api/v1/workspaces/other-workspace/management-capabilities/").status_code == 403


def test_page_create_converts_before_persistence(api_key_client, workspace, project, monkeypatch):
    post = Mock(
        return_value=Mock(
            status_code=200,
            headers={},
            json=lambda: {
                "description_html": "<p>Canonical ready</p>",
                "description_json": {"type": "doc", "content": []},
                "description_binary": "AQIDBA==",
            },
        )
    )
    monkeypatch.setattr("plane.api.views.management.requests.post", post)
    response = api_key_client.post(
        project_url(workspace, project) + "pages/",
        {
            "name": "Release 1",
            "description_html": "<p>Ready</p>",
        },
        format="json",
    )
    assert response.status_code == 201, response.data
    created = Page.objects.get(id=response.data["id"])
    assert bytes(created.description_binary) == b"\x01\x02\x03\x04"
    assert created.description_html == "<p>Canonical ready</p>"
    assert ProjectPage.objects.filter(page=created, project=project).exists()
    assert response["ETag"] == f'"{page_revision(created)}"'
    assert post.call_args.kwargs["json"]["variant"] == "document"


def test_failed_codec_creates_no_page(api_key_client, workspace, project, monkeypatch):
    monkeypatch.setattr(
        "plane.api.views.management.requests.post",
        Mock(
            return_value=Mock(
                status_code=503,
                headers={},
                json=lambda: {"error": "unavailable"},
            )
        ),
    )
    response = api_key_client.post(
        project_url(workspace, project) + "pages/",
        {
            "name": "Not created",
            "description_html": "<p>Draft</p>",
        },
        format="json",
    )
    assert response.status_code == 503
    assert not Page.objects.filter(name="Not created").exists()


def test_malformed_codec_response_creates_no_page(api_key_client, workspace, project, monkeypatch):
    monkeypatch.setattr(
        "plane.api.views.management.requests.post",
        Mock(return_value=Mock(status_code=200, headers={}, json=lambda: {"unexpected": True})),
    )
    response = api_key_client.post(project_url(workspace, project) + "pages/", {"name": "Malformed"}, format="json")
    assert response.status_code == 502
    assert response.data["outcome"] == "not_applied"
    assert not Page.objects.filter(name="Malformed").exists()


def test_child_page_retrievable_and_listed(api_key_client, workspace, project, page, create_user):
    child = Page.objects.create(workspace=workspace, owned_by=create_user, name="Child", parent=page)
    ProjectPage.objects.create(workspace=workspace, project=project, page=child)
    response = api_key_client.get(page_url(workspace, project, child))
    assert response.status_code == 200, response.data
    response = api_key_client.get(project_url(workspace, project) + "pages/")
    assert {str(item["id"]) for item in response.data} == {str(page.id), str(child.id)}


def test_page_cross_project_denied(api_key_client, workspace, project, page, create_user):
    other = Project.objects.create(workspace=workspace, name="Other", identifier="OTH")
    ProjectMember.objects.create(workspace=workspace, project=other, member=create_user, role=20)
    assert api_key_client.get(page_url(workspace, other, page)).status_code == 403
    assert api_key_client.get(page_url(workspace, other, page) + "description/").status_code == 403


def test_guest_cannot_retrieve_page_hidden_by_project_setting(api_key_client, workspace, project, page, create_user):
    other = User.objects.create(email="owner@plane.test", username="other-page-owner")
    page.owned_by = other
    page.access = Page.PUBLIC_ACCESS
    page.save()
    project.guest_view_all_features = False
    project.save()
    ProjectMember.objects.filter(project=project, member=create_user).update(role=5)
    assert api_key_client.get(page_url(workspace, project, page)).status_code == 404
    assert api_key_client.get(page_url(workspace, project, page) + "description/").status_code == 404


def test_page_metadata_revision_and_content_separation(api_key_client, workspace, project, page):
    url = page_url(workspace, project, page)
    original = api_key_client.get(url)
    first = api_key_client.patch(url, {"color": "#123456"}, format="json", HTTP_IF_MATCH=original["ETag"])
    assert first.status_code == 200, first.data
    assert first["ETag"] != original["ETag"]
    second = api_key_client.patch(url, {"color": "#654321"}, format="json", HTTP_IF_MATCH=original["ETag"])
    assert second.status_code == 412, second.data
    page.refresh_from_db()
    assert page.color == "#123456"
    assert api_key_client.patch(url, {"description_html": "<p>Wrong path</p>"}, format="json").status_code == 400


def test_content_proxy_requires_current_revision(api_key_client, workspace, project, page, monkeypatch):
    post = Mock(
        return_value=Mock(status_code=200, headers={"ETag": 'W/"express-hash"'}, json=lambda: {"updated_at": "updated"})
    )
    monkeypatch.setattr("plane.api.views.management.requests.post", post)
    url = page_url(workspace, project, page) + "description/"
    assert (
        api_key_client.patch(
            url, {"description_html": "<p>New</p>"}, format="json", HTTP_IF_MATCH='"stale"'
        ).status_code
        == 412
    )
    post.assert_not_called()
    response = api_key_client.patch(url, {"description_html": "<p>New</p>"}, format="json")
    assert response.status_code == 200
    payload = post.call_args.kwargs["json"]
    assert payload["base_description_binary"] == base64.b64encode(bytes(page.description_binary)).decode()
    assert payload["expected_revision"] == page_revision(page)
    assert payload["base_description_html"] == page.description_html
    assert response["ETag"] == '"updated"'


@pytest.mark.django_db(transaction=True)
def test_concurrent_metadata_updates_only_one_wins(api_token, workspace, project, page):
    barrier = Barrier(2)
    revision = f'"{page_revision(page)}"'
    url = page_url(workspace, project, page)

    def update(color):
        client = APIClient()
        client.credentials(HTTP_X_API_KEY=api_token.token)
        try:
            barrier.wait(timeout=5)
            return client.patch(url, {"color": color}, format="json", HTTP_IF_MATCH=revision).status_code
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["#123456", "#654321"]))
    assert sorted(results) == [200, 412]


def test_live_persistence_validates_key_secret_and_revision(api_key_client, workspace, project, page):
    url = page_url(workspace, project, page) + "description/"
    payload = {
        "description_html": "<p>After</p>",
        "description_json": {"type": "doc"},
        "description_binary": "BAQDAg==",
    }
    expected = f'"{page_revision(page)}"'
    anonymous = APIClient()
    assert anonymous.patch(url, payload, format="json", HTTP_LIVE_SERVER_SECRET_KEY="test-live-secret").status_code in (
        401,
        403,
    )
    assert api_key_client.patch(url, payload, format="json", HTTP_LIVE_SERVER_SECRET_KEY="wrong").status_code == 403
    response = api_key_client.patch(
        url, payload, format="json", HTTP_LIVE_SERVER_SECRET_KEY="test-live-secret", HTTP_IF_MATCH=expected
    )
    assert response.status_code == 200, response.data
    page.refresh_from_db()
    assert page.description_html == "<p>After</p>"
    assert bytes(page.description_binary) == b"\x04\x04\x03\x02"
    stale = api_key_client.patch(
        url, payload, format="json", HTTP_LIVE_SERVER_SECRET_KEY="test-live-secret", HTTP_IF_MATCH=expected
    )
    assert stale.status_code == 412
    assert api_key_client.get(url)["ETag"] == response["ETag"]


def test_page_archive_lock_and_delete(api_key_client, workspace, project, page):
    url = page_url(workspace, project, page)
    assert api_key_client.delete(url).status_code == 400
    assert api_key_client.post(url + "lock/").status_code == 204
    assert api_key_client.patch(url, {"name": "Locked"}, format="json").status_code == 409
    assert api_key_client.delete(url + "lock/").status_code == 204
    assert api_key_client.post(url + "archive/").status_code == 200
    assert api_key_client.delete(url + "archive/").status_code == 204
    assert api_key_client.post(url + "archive/").status_code == 200
    assert api_key_client.delete(url).status_code == 204
    assert not Page.objects.filter(id=page.id).exists()


def test_page_summary_versions_favorite_duplicate(api_key_client, workspace, project, page, create_user):
    version = PageVersion.objects.create(
        workspace=workspace, page=page, owned_by=create_user, description_html="<p>Old</p>"
    )
    root = project_url(workspace, project)
    url = page_url(workspace, project, page)
    assert api_key_client.get(root + "pages-summary/").status_code == 200
    assert api_key_client.get(url + "versions/").status_code == 200
    assert api_key_client.get(url + f"versions/{version.id}/").status_code == 200
    assert api_key_client.post(root + f"favorite-pages/{page.id}/").status_code == 204
    assert api_key_client.delete(root + f"favorite-pages/{page.id}/").status_code == 204
    copied = api_key_client.post(url + "duplicate/")
    assert copied.status_code == 201, copied.data
    assert copied.data["id"] != str(page.id)


def test_workspace_member_lifecycle(api_key_client, workspace):
    user = User.objects.create(email="member@plane.test", username="member-lifecycle")
    response = api_key_client.post(member_url(workspace), {"member_id": str(user.id), "role": 15}, format="json")
    assert response.status_code == 201, response.data
    membership_id = response.data["id"]
    duplicate = api_key_client.post(member_url(workspace), {"member_id": str(user.id)}, format="json")
    assert duplicate.status_code == 409
    url = member_url(workspace) + f"{membership_id}/"
    assert api_key_client.get(url).status_code == 200
    assert api_key_client.patch(url, {"role": 5}, format="json").status_code == 200
    assert WorkspaceMember.objects.get(id=membership_id).role == 5
    assert api_key_client.delete(url).status_code == 204
    assert WorkspaceMember.objects.get(id=membership_id).is_active is False


def test_workspace_settings_update(api_key_client, workspace):
    response = api_key_client.patch(f"/api/v1/workspaces/{workspace.slug}/", {"name": "Renamed"}, format="json")
    assert response.status_code == 200, response.data
    workspace.refresh_from_db()
    assert workspace.name == "Renamed"


def test_webhook_lifecycle(api_key_client, workspace, monkeypatch):
    monkeypatch.setattr("plane.app.serializers.webhook.validate_url", lambda *a, **kw: None)
    url = f"/api/v1/workspaces/{workspace.slug}/webhooks/"
    created = api_key_client.post(url, {"url": "https://events.example.test/hook", "issue": True}, format="json")
    assert created.status_code == 201, created.data
    detail = url + f"{created.data['id']}/"
    assert api_key_client.get(detail).status_code == 200
    assert api_key_client.patch(detail, {"is_active": False}, format="json").status_code == 200
    regenerated = api_key_client.post(detail + "regenerate/")
    assert regenerated.status_code == 200
    assert regenerated.data["secret_key"] != created.data["secret_key"]
    logs = f"/api/v1/workspaces/{workspace.slug}/webhook-logs/{created.data['id']}/"
    assert api_key_client.get(logs).status_code == 200
    assert api_key_client.delete(detail).status_code == 204


def test_work_item_archive_and_relation_delete(api_key_client, workspace, project):
    state = State.objects.create(workspace=workspace, project=project, name="Done", group="completed")
    first = Issue.objects.create(workspace=workspace, project=project, state=state, name="First")
    second = Issue.objects.create(workspace=workspace, project=project, state=state, name="Second")
    relation = IssueRelation.objects.create(
        workspace=workspace, project=project, issue=first, related_issue=second, relation_type="relates_to"
    )
    url = project_url(workspace, project) + f"work-items/{first.id}/"
    assert api_key_client.delete(url + f"relations/{relation.id}/").status_code == 204
    assert not IssueRelation.objects.filter(id=relation.id).exists()
    assert api_key_client.post(url + "archive/").status_code == 200
    first.refresh_from_db()
    assert first.archived_at is not None
    assert api_key_client.delete(url + "archive/").status_code == 204
    first.refresh_from_db()
    assert first.archived_at is None


def test_workspace_scoped_key_cannot_cross_memberships(api_key_client, api_token, workspace, create_user):
    other = Workspace.objects.create(name="Other scope", slug="other-scope", owner=create_user)
    WorkspaceMember.objects.create(workspace=other, member=create_user, role=20)
    api_token.workspace = workspace
    api_token.save()
    assert api_key_client.get(f"/api/v1/workspaces/{other.slug}/management-capabilities/").status_code == 403
    response = api_key_client.get("/api/v1/workspaces/")
    data = response.data.get("results", response.data) if isinstance(response.data, dict) else response.data
    assert [item["slug"] for item in data] == [workspace.slug]
    assert (
        api_key_client.post("/api/v1/workspaces/", {"name": "Denied", "slug": "denied"}, format="json").status_code
        == 403
    )


def test_rename_uses_live_title_transaction(api_key_client, workspace, project, page, monkeypatch):
    post = Mock(return_value=Mock(status_code=200, headers={}, json=lambda: {"name": "Renamed"}))
    monkeypatch.setattr("plane.api.views.management.requests.post", post)
    url = page_url(workspace, project, page)
    response = api_key_client.patch(url, {"name": "Renamed"}, format="json")
    assert response.status_code == 200
    payload = post.call_args.kwargs["json"]
    assert payload["new_name"] == "Renamed"
    assert "description_html" not in payload
    assert payload["name"] == page.name
    page.refresh_from_db()
    assert page.name == "Build status"
    assert api_key_client.patch(url, {"name": "Mixed", "color": "red"}, format="json").status_code == 400


def test_content_commit_generates_real_version(
    api_key_client, workspace, project, page, monkeypatch, django_capture_on_commit_callbacks
):
    from plane.bgtasks.page_version_task import track_page_version

    monkeypatch.setattr(track_page_version, "delay", track_page_version.run)
    with django_capture_on_commit_callbacks(execute=True):
        response = api_key_client.patch(
            page_url(workspace, project, page) + "description/",
            {
                "name": "Atomic rename",
                "description_html": "<p>Versioned</p>",
                "description_json": {"type": "doc", "content": []},
                "description_binary": "AQIDBA==",
            },
            format="json",
            HTTP_LIVE_SERVER_SECRET_KEY="test-live-secret",
        )
        assert not PageVersion.objects.filter(page=page).exists()
    assert response.status_code == 200, response.data
    version = PageVersion.objects.get(page=page)
    assert version.description_html == "<p>Versioned</p>"
    assert version.description_json == {"type": "doc", "content": []}
    page.refresh_from_db()
    assert page.name == response.data["name"] == "Atomic rename"


def test_live_title_save_does_not_reenter_proxy(api_key_client, workspace, project, page, monkeypatch):
    post = Mock()
    monkeypatch.setattr("plane.api.views.management.requests.post", post)
    url = page_url(workspace, project, page)
    assert (
        api_key_client.patch(
            url, {"name": "Bad secret"}, format="json", HTTP_LIVE_SERVER_SECRET_KEY="wrong"
        ).status_code
        == 403
    )
    response = api_key_client.patch(
        url, {"name": "Editor title"}, format="json", HTTP_LIVE_SERVER_SECRET_KEY="test-live-secret"
    )
    assert response.status_code == 200, response.data
    page.refresh_from_db()
    assert page.name == response.data["name"] == "Editor title"
    post.assert_not_called()


def test_compressed_response_revision_can_be_reused(api_key_client, workspace, project, page):
    url = page_url(workspace, project, page)
    response = api_key_client.get(url, HTTP_ACCEPT_ENCODING="gzip")
    assert response.status_code == 200
    assert response["ETag"].startswith('W/"')
    updated = api_key_client.patch(url, {"color": "blue"}, format="json", HTTP_IF_MATCH=response["ETag"])
    assert updated.status_code == 200, updated.data


@pytest.mark.parametrize(
    "relation_type,reverse_type",
    [
        ("blocking", "blocked_by"),
        ("blocked_by", "blocking"),
        ("duplicate", "duplicate"),
        ("relates_to", "relates_to"),
        ("start_before", "start_after"),
        ("start_after", "start_before"),
        ("finish_before", "finish_after"),
        ("finish_after", "finish_before"),
    ],
)
def test_relation_discover_then_delete_from_either_end(
    api_key_client, workspace, project, create_user, relation_type, reverse_type
):
    state = State.objects.create(workspace=workspace, project=project, name="Open", group="started")
    first = Issue.objects.create(workspace=workspace, project=project, state=state, name="First relation endpoint")
    other_project = Project.objects.create(workspace=workspace, name="Related project", identifier="REL")
    ProjectMember.objects.create(workspace=workspace, project=other_project, member=create_user, role=20)
    other_state = State.objects.create(workspace=workspace, project=other_project, name="Open", group="started")
    second = Issue.objects.create(
        workspace=workspace, project=other_project, state=other_state, name="Second relation endpoint"
    )
    first_url = project_url(workspace, project) + f"work-items/{first.id}/relations/"
    second_url = project_url(workspace, other_project) + f"work-items/{second.id}/relations/"
    created = api_key_client.post(
        first_url, {"relation_type": relation_type, "issues": [str(second.id)]}, format="json"
    )
    assert created.status_code == 201, created.data
    assert len(created.data) == 1
    relation_id = str(created.data[0]["relation_id"])
    assert str(created.data[0]["id"]) == str(second.id)
    assert str(created.data[0]["project_id"]) == str(other_project.id)
    assert relation_id != str(second.id)
    expected_first = {"project_id": str(other_project.id), "issue_id": str(second.id), "relation_id": relation_id}
    expected_second = {"project_id": str(project.id), "issue_id": str(first.id), "relation_id": relation_id}
    first_list = api_key_client.get(first_url)
    second_list = api_key_client.get(second_url)
    assert first_list.status_code == second_list.status_code == 200
    assert first_list.data[relation_type] == [expected_first]
    assert second_list.data[reverse_type] == [expected_second]
    # Exercise both endpoint orientations using the ID obtained exclusively via API.
    discovered = (
        second_list.data[reverse_type][0]
        if relation_type in ("blocking", "start_after", "finish_after")
        else first_list.data[relation_type][0]
    )
    delete_url = second_url if relation_type in ("blocking", "start_after", "finish_after") else first_url
    assert api_key_client.delete(delete_url + discovered["relation_id"] + "/").status_code == 204
    assert all(not refs for refs in api_key_client.get(first_url).data.values())
    assert all(not refs for refs in api_key_client.get(second_url).data.values())


@pytest.fixture
def storage_client(monkeypatch):
    # Keep the real S3Storage constructor so unsupported keyword arguments fail.
    client = Mock()
    client.generate_presigned_post.return_value = {"url": "https://storage.test/uploads", "fields": {"key": "test-key"}}
    client.generate_presigned_url.return_value = "https://storage.test/download"
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.setenv("AWS_S3_BUCKET_NAME", "test-uploads")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("USE_MINIO", "1")
    monkeypatch.setattr("plane.settings.storage.boto3.client", Mock(return_value=client))
    return client


def test_generic_asset_presign_confirm_download_delete(api_key_client, workspace, project, storage_client):
    root = f"/api/v1/workspaces/{workspace.slug}/assets/"
    response = api_key_client.post(
        root, {"name": "report.txt", "type": "text/plain", "size": 16, "project_id": str(project.id)}, format="json"
    )
    assert response.status_code == 200, response.data
    asset_id = response.data["asset_id"]
    asset = FileAsset.objects.get(id=asset_id)
    assert response.data["upload_data"]["url"] == "https://storage.test/uploads"
    assert storage_client.generate_presigned_post.call_args.kwargs["Key"] == asset.asset.name
    detail = root + asset_id + "/"
    assert api_key_client.get(detail).status_code == 400
    assert api_key_client.patch(detail, {"is_uploaded": True}, format="json").status_code == 204
    download = api_key_client.get(detail)
    assert download.status_code == 200, download.data
    assert download.data["asset_url"] == "https://storage.test/download"
    assert storage_client.generate_presigned_url.call_args.kwargs["Params"]["Key"] == asset.asset.name
    assert api_key_client.delete(detail).status_code == 204
    assert api_key_client.get(detail).status_code == 404
    deleted = FileAsset.all_objects.get(id=asset_id)
    assert deleted.is_deleted and deleted.deleted_at is not None


def test_generic_asset_delete_respects_project_and_entity_type(api_key_client, workspace, project, create_user):
    other_project = Project.objects.create(workspace=workspace, name="Private assets", identifier="PVT")
    asset = FileAsset.objects.create(
        workspace=workspace,
        project=other_project,
        asset="test-asset",
        entity_type=FileAsset.EntityTypeContext.ISSUE_ATTACHMENT,
    )
    root = f"/api/v1/workspaces/{workspace.slug}/assets/"
    assert api_key_client.delete(root + str(asset.id) + "/").status_code == 403
    logo = FileAsset.objects.create(
        workspace=workspace, asset="test-logo", entity_type=FileAsset.EntityTypeContext.WORKSPACE_LOGO
    )
    assert api_key_client.delete(root + str(logo.id) + "/").status_code == 404
    assert FileAsset.objects.filter(id__in=[asset.id, logo.id]).count() == 2


def test_user_server_asset_uses_supported_storage_constructor(api_key_client, storage_client):
    response = api_key_client.post(
        "/api/v1/assets/user-assets/server/",
        {"name": "avatar.png", "type": "image/png", "size": 16, "entity_type": "USER_AVATAR"},
        format="json",
    )
    assert response.status_code == 200, response.data
    assert response.data["upload_data"]["url"] == "https://storage.test/uploads"


def test_unscoped_key_workspace_create_rename_read_delete(api_key_client):
    created = api_key_client.post(
        "/api/v1/workspaces/", {"name": "Disposable acceptance", "slug": "disposable-acceptance"}, format="json"
    )
    assert created.status_code == 201, created.data
    workspace_id = created.data["id"]
    detail = "/api/v1/workspaces/disposable-acceptance/"
    assert api_key_client.patch(detail, {"name": "Renamed acceptance"}, format="json").status_code == 200
    assert api_key_client.get(detail).data["name"] == "Renamed acceptance"
    assert api_key_client.delete(detail).status_code == 204
    assert api_key_client.get(detail).status_code in (403, 404)
    assert not Workspace.objects.filter(id=workspace_id).exists()
