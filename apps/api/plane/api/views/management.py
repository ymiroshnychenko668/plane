# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""API-key access to CE management operations already available in the app.

Keep the app's domain logic and permission checks. Only authentication and the
machine-facing document contract differ. Content writes pass through live so an
open Yjs document cannot silently restore content overwritten through the API.
"""

import base64
import json
import os
import secrets

import requests
from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response

from plane.api.middleware.api_authentication import APIKeyAuthentication
from plane.api.rate_limit import ApiKeyRateThrottle
from plane.app.permissions import (
    ROLE,
    ProjectEntityPermission,
    ProjectPagePermission,
    WorkspaceEntityPermission,
    allow_permission,
)
from plane.app.serializers import PageBinaryUpdateSerializer, PageDetailSerializer, PageSerializer
from plane.app.serializers.workspace import WorkspaceMemberAdminSerializer
from plane.app.views.base import BaseViewSet
from plane.app.views.issue.archive import IssueArchiveViewSet
from plane.app.views.page.base import PageViewSet, PageFavoriteViewSet, PageDuplicateEndpoint, PagesDescriptionViewSet
from plane.app.views.page.version import PageVersionEndpoint
from plane.app.views.webhook.base import WebhookEndpoint, WebhookLogsEndpoint, WebhookSecretRegenerateEndpoint
from plane.app.views.workspace.base import WorkSpaceViewSet
from plane.app.views.workspace.member import WorkSpaceMemberViewSet
from plane.bgtasks.page_transaction_task import page_transaction
from plane.bgtasks.page_version_task import track_page_version
from plane.bgtasks.issue_activities_task import issue_activity
from plane.app.serializers import IssueRelationSerializer
from plane.db.models import (
    APIToken,
    Issue,
    IssueRelation,
    Page,
    Project,
    ProjectMember,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.utils.host import base_host

from .base import BaseAPIView


class LiveUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Plane live service is unavailable; no content write was attempted."
    default_code = "live_unavailable"


class RevisionConflict(APIException):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    default_detail = "The page changed. Read its current revision before retrying."
    default_code = "revision_conflict"


def page_revision(page):
    return page.updated_at.isoformat()


def check_revision(request, page):
    expected = request.headers.get("If-Match")
    # Django's GZipMiddleware turns the revision tag into W/"..." for a
    # compressed response. The tag represents a model revision, so normalize
    # that transport marker before comparing it to the persisted revision.
    if expected:
        expected = expected.strip().removeprefix("W/").strip('"')
        if expected != "*" and expected != page_revision(page):
            raise RevisionConflict()


def content_data(page):
    return {
        "id": str(page.id),
        "name": page.name,
        "description_html": page.description_html,
        "description_json": page.description_json,
        "description_binary": base64.b64encode(bytes(page.description_binary or b"")).decode(),
        "updated_at": page_revision(page),
    }


def content_response(page, response_status=status.HTTP_200_OK):
    response = Response(content_data(page), status=response_status)
    response["ETag"] = f'"{page_revision(page)}"'
    return response


def call_live(path, payload, mutation=True):
    if not settings.LIVE_URL:
        raise LiveUnavailable()
    secret = os.environ.get("LIVE_SERVER_SECRET_KEY", "")
    if not secret:
        raise LiveUnavailable("LIVE_SERVER_SECRET_KEY is not configured.")
    try:
        response = requests.post(
            f"{settings.LIVE_URL.rstrip('/')}/{path.lstrip('/')}",
            json=payload,
            headers={"live-server-secret-key": secret},
            timeout=(5, 30),
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        # A timeout after sending content can have an unknown result. The
        # caller must reconcile rather than replaying the mutation blindly.
        return Response(
            {"error": "Live response unavailable", "outcome": "unknown" if mutation else "not_applied"},
            status=status.HTTP_502_BAD_GATEWAY,
        )
    result = Response(data, status=response.status_code)
    if isinstance(data, dict) and data.get("updated_at"):
        # Express may generate a representation hash. Expose the persisted
        # model revision, matching GET and the callback's If-Match contract.
        result["ETag"] = f'"{data["updated_at"]}"'
    return result


class ManagementAPIKeyAuthentication(APIKeyAuthentication):
    def authenticate(self, request):
        result = super().authenticate(request)
        if result is not None:
            token = APIToken.objects.select_related("workspace").get(token=result[1])
            request.api_key_workspace_id = token.workspace_id
            slug = request.parser_context.get("kwargs", {}).get("slug")
            if token.workspace_id and slug and token.workspace.slug != slug:
                raise PermissionDenied("This API key belongs to another workspace.")
        return result


class APIKeyManagementMixin:
    authentication_classes = [ManagementAPIKeyAuthentication]
    use_read_replica = False

    def get_throttles(self):
        return [ApiKeyRateThrottle()]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        for name in ("X-RateLimit-Remaining", "X-RateLimit-Reset"):
            if name in request.META:
                response[name] = request.META[name]
        return response


class PageManagementViewSet(APIKeyManagementMixin, PageViewSet):
    """All project pages, including children and archived pages."""

    def get_queryset(self):
        queryset = (
            Page.objects.filter(
                workspace__slug=self.kwargs["slug"],
                project_pages__project_id=self.kwargs["project_id"],
                project_pages__deleted_at__isnull=True,
            )
            .filter(Q(owned_by=self.request.user) | Q(access=Page.PUBLIC_ACCESS))
            .prefetch_related("projects", "labels")
            .select_related("workspace", "owned_by")
            .order_by("-created_at")
            .distinct()
        )
        project = Project.objects.get(id=self.kwargs["project_id"], workspace__slug=self.kwargs["slug"])
        guest = ProjectMember.objects.filter(project=project, member=self.request.user, is_active=True, role=5).exists()
        if guest and not project.guest_view_all_features:
            queryset = queryset.filter(owned_by=self.request.user)
        return queryset

    def create(self, request, slug, project_id):
        project = get_object_or_404(Project, id=project_id, workspace__slug=slug)
        parent = request.data.get("parent")
        if parent:
            get_object_or_404(self.get_queryset(), id=parent)
        html = request.data.get("description_html", "<p></p>")
        validated = PageBinaryUpdateSerializer(data={"description_html": html})
        validated.is_valid(raise_exception=True)
        html = validated.validated_data["description_html"] or "<p></p>"
        converted = call_live("convert-document/", {"description_html": html, "variant": "document"}, mutation=False)
        if converted.status_code != status.HTTP_200_OK:
            return converted
        if not isinstance(converted.data, dict) or not {
            "description_html",
            "description_json",
            "description_binary",
        }.issubset(converted.data):
            return Response({"error": "Invalid document codec response", "outcome": "not_applied"}, status=502)
        content = PageBinaryUpdateSerializer(data=converted.data)
        content.is_valid(raise_exception=True)
        serializer = PageSerializer(
            data=request.data,
            context={
                "project_id": project.id,
                "owned_by_id": request.user.id,
                **content.validated_data,
            },
        )
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            page = serializer.save()
            transaction.on_commit(
                lambda: page_transaction.delay(
                    new_description_html=page.description_html, old_description_html=None, page_id=str(page.id)
                )
            )
        response = Response({**PageDetailSerializer(page).data, **content_data(page)}, status=status.HTTP_201_CREATED)
        response["ETag"] = f'"{page_revision(page)}"'
        return response

    def retrieve(self, request, slug, project_id, page_id=None):
        page = get_object_or_404(self.get_queryset(), id=page_id)
        response = Response({**PageDetailSerializer(page).data, **content_data(page)})
        response["ETag"] = f'"{page_revision(page)}"'
        return response

    def partial_update(self, request, slug, project_id, page_id):
        if any(key.startswith("description_") for key in request.data):
            raise ValidationError({"detail": "Use the description endpoint to update page content."})
        supplied_secret = request.headers.get("live-server-secret-key")
        if supplied_secret is not None:
            configured_secret = os.environ.get("LIVE_SERVER_SECRET_KEY", "")
            if not configured_secret or not secrets.compare_digest(supplied_secret, configured_secret):
                return Response({"error": "Invalid live credential"}, status=status.HTTP_403_FORBIDDEN)
            if set(request.data) != {"name"}:
                raise ValidationError({"detail": "Live metadata persistence only accepts the title."})
            # TitleSync uses this after a browser edit on a document originally
            # loaded by an API connection. Its Yjs title has already changed.
            with transaction.atomic():
                page = get_object_or_404(Page.objects.select_for_update(), id=page_id, workspace__slug=slug)
                check_revision(request, page)
                if page.is_locked or page.archived_at:
                    return Response({"error": "Page is locked or archived"}, status=status.HTTP_409_CONFLICT)
                serializer = PageDetailSerializer(page, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                serializer.save()
                return content_response(page)
        if "name" in request.data:
            if set(request.data) != {"name"}:
                raise ValidationError({"detail": "Rename the page separately from other metadata changes."})
            page = get_object_or_404(self.get_queryset(), id=page_id)
            check_revision(request, page)
            if page.is_locked or page.archived_at:
                return Response({"error": "Page is locked or archived"}, status=status.HTTP_409_CONFLICT)
            serializer = PageDetailSerializer(page, data=request.data, partial=True)
            serializer.is_valid(raise_exception=True)
            return proxy_page_content(request, page, slug, project_id, new_name=serializer.validated_data["name"])
        with transaction.atomic():
            page = get_object_or_404(Page.objects.select_for_update(), id=page_id, workspace__slug=slug)
            check_revision(request, page)
            response = super().partial_update(request, slug, project_id, page_id)
            if response.status_code == status.HTTP_200_OK:
                page.refresh_from_db()
                response["ETag"] = f'"{page_revision(page)}"'
            return response

    def _mutate(self, request, slug, project_id, page_id, action, touch=False):
        with transaction.atomic():
            page = get_object_or_404(Page.objects.select_for_update(), id=page_id, workspace__slug=slug)
            check_revision(request, page)
            response = action(request, slug, project_id, page_id)
            if response.status_code < 300 and self.get_queryset().filter(id=page_id).exists():
                if touch:
                    Page.objects.filter(id=page_id).update(updated_at=timezone.now())
                page.refresh_from_db()
                response["ETag"] = f'"{page_revision(page)}"'
            return response

    def archive(self, request, slug, project_id, page_id):
        return self._mutate(request, slug, project_id, page_id, super().archive, touch=True)

    def unarchive(self, request, slug, project_id, page_id):
        return self._mutate(request, slug, project_id, page_id, super().unarchive, touch=True)

    def lock(self, request, slug, project_id, page_id):
        return self._mutate(request, slug, project_id, page_id, super().lock)

    def unlock(self, request, slug, project_id, page_id):
        return self._mutate(request, slug, project_id, page_id, super().unlock)

    def access(self, request, slug, project_id, page_id):
        if request.data.get("access") not in (Page.PUBLIC_ACCESS, Page.PRIVATE_ACCESS):
            raise ValidationError({"access": "Choose 0 (public) or 1 (private)."})
        return self._mutate(request, slug, project_id, page_id, super().access)

    def destroy(self, request, slug, project_id, page_id):
        return self._mutate(request, slug, project_id, page_id, super().destroy)


def proxy_page_content(request, page, slug, project_id, description_html=None, new_name=None):
    payload = {
        "workspace_slug": slug,
        "project_id": str(project_id),
        "user_id": str(request.user.id),
        "api_key": request.headers.get("X-Api-Key"),
        "description_html": description_html if description_html is not None else page.description_html,
        "expected_revision": page_revision(page),
        "base_description_binary": content_data(page)["description_binary"],
        "base_description_html": page.description_html,
        "name": page.name,
    }
    if new_name is not None:
        payload["new_name"] = new_name
        if description_html is None:
            payload.pop("description_html")
    return call_live(f"documents/{page.id}/content/", payload)


class PageContentAPIEndpoint(APIKeyManagementMixin, PagesDescriptionViewSet):
    def get_queryset(self):
        return PageManagementViewSet.get_queryset(self)

    def retrieve(self, request, slug, project_id, page_id):
        page = get_object_or_404(self.get_queryset(), id=page_id)
        return content_response(page)

    def partial_update(self, request, slug, project_id, page_id):
        supplied_secret = request.headers.get("live-server-secret-key")
        if supplied_secret is not None:
            configured_secret = os.environ.get("LIVE_SERVER_SECRET_KEY", "")
            if not configured_secret or not secrets.compare_digest(supplied_secret, configured_secret):
                return Response({"error": "Invalid live credential"}, status=status.HTTP_403_FORBIDDEN)
            required = {"description_html", "description_json", "description_binary"}
            if not required.issubset(request.data):
                raise ValidationError({"detail": "Live persistence requires all document formats."})
            with transaction.atomic():
                page = get_object_or_404(Page.objects.select_for_update(), id=page_id, workspace__slug=slug)
                check_revision(request, page)
                if page.is_locked or page.archived_at:
                    return Response({"error": "Page is locked or archived"}, status=status.HTTP_409_CONFLICT)
                old_html = page.description_html
                serializer = PageBinaryUpdateSerializer(page, data=request.data, partial=True)
                serializer.is_valid(raise_exception=True)
                if "name" in request.data:
                    metadata = PageDetailSerializer(page, data={"name": request.data["name"]}, partial=True)
                    metadata.is_valid(raise_exception=True)
                    page.name = metadata.validated_data["name"]
                serializer.save()
                transaction.on_commit(
                    lambda: page_transaction.delay(
                        new_description_html=page.description_html,
                        old_description_html=old_html,
                        page_id=str(page.id),
                    )
                )
                transaction.on_commit(
                    lambda: track_page_version.delay(
                        page_id=str(page.id),
                        existing_instance=json.dumps({"description_html": old_html}),
                        user_id=str(request.user.id),
                    )
                )
                return content_response(page)

        page = get_object_or_404(self.get_queryset(), id=page_id)
        check_revision(request, page)
        if page.is_locked or page.archived_at:
            return Response({"error": "Page is locked or archived"}, status=status.HTTP_409_CONFLICT)
        serializer = PageBinaryUpdateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if "description_html" not in serializer.validated_data:
            raise ValidationError({"description_html": "This field is required."})
        return proxy_page_content(
            request,
            page,
            slug,
            project_id,
            description_html=serializer.validated_data["description_html"] or "<p></p>",
        )


class PageVersionAPIEndpoint(APIKeyManagementMixin, PageVersionEndpoint):
    pass


class PageDuplicateAPIEndpoint(APIKeyManagementMixin, PageDuplicateEndpoint):
    pass


class PageFavoriteAPIEndpoint(APIKeyManagementMixin, PageFavoriteViewSet):
    permission_classes = [ProjectPagePermission]


class WorkItemArchiveAPIEndpoint(APIKeyManagementMixin, IssueArchiveViewSet):
    pass


class WorkItemRelationDetailAPIEndpoint(APIKeyManagementMixin, BaseAPIView):
    permission_classes = [ProjectEntityPermission]

    def delete(self, request, slug, project_id, issue_id, pk):
        get_object_or_404(Issue, id=issue_id, project_id=project_id, workspace__slug=slug)
        relation = get_object_or_404(
            IssueRelation.objects.filter(Q(issue_id=issue_id) | Q(related_issue_id=issue_id)),
            id=pk,
            workspace__slug=slug,
        )
        current_instance = json.dumps(IssueRelationSerializer(relation).data, cls=DjangoJSONEncoder)
        relation.delete()
        issue_activity.delay(
            type="issue_relation.activity.deleted",
            requested_data="{}",
            actor_id=str(request.user.id),
            issue_id=str(issue_id),
            project_id=str(project_id),
            current_instance=current_instance,
            epoch=int(timezone.now().timestamp()),
            notification=True,
            origin=base_host(request=request, is_app=False),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class WorkspaceManagementViewSet(APIKeyManagementMixin, WorkSpaceViewSet):
    def get_queryset(self):
        queryset = super().get_queryset()
        workspace_id = getattr(self.request, "api_key_workspace_id", None)
        return queryset.filter(id=workspace_id) if workspace_id else queryset

    def create(self, request, *args, **kwargs):
        if getattr(request, "api_key_workspace_id", None):
            raise PermissionDenied("A workspace-scoped key cannot create another workspace.")
        return super().create(request, *args, **kwargs)

    def list(self, request, *args, **kwargs):
        # The app list decorator requires a slug, but discovery has no slug.
        # WorkSpaceViewSet.get_queryset already scopes to active memberships.
        return BaseViewSet.list(self, request, *args, **kwargs)


class WorkspaceMemberCreateSerializer(serializers.Serializer):
    member_id = serializers.PrimaryKeyRelatedField(queryset=User.objects.filter(is_active=True))
    role = serializers.ChoiceField(choices=[5, 15, 20], default=15)


class WorkspaceMemberManagementViewSet(APIKeyManagementMixin, WorkSpaceMemberViewSet):
    @allow_permission([ROLE.ADMIN], level="WORKSPACE")
    def create(self, request, slug):
        serializer = WorkspaceMemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        workspace = get_object_or_404(Workspace, slug=slug)
        member = serializer.validated_data["member_id"]
        with transaction.atomic():
            membership, created = WorkspaceMember.objects.get_or_create(
                workspace=workspace,
                member=member,
                defaults={"role": serializer.validated_data["role"]},
            )
            if not created:
                return Response(
                    {"error": "Membership already exists", "id": str(membership.id)},
                    status=status.HTTP_409_CONFLICT,
                )
        return Response(WorkspaceMemberAdminSerializer(membership).data, status=status.HTTP_201_CREATED)


class WebhookManagementAPIEndpoint(APIKeyManagementMixin, WebhookEndpoint):
    pass


class WebhookLogsAPIEndpoint(APIKeyManagementMixin, WebhookLogsEndpoint):
    pass


class WebhookSecretAPIEndpoint(APIKeyManagementMixin, WebhookSecretRegenerateEndpoint):
    pass


class ManagementCapabilitiesAPIEndpoint(APIKeyManagementMixin, BaseAPIView):
    permission_classes = [WorkspaceEntityPermission]

    def get(self, request, slug):
        return Response(
            {
                "extension": "plane-ce-management",
                "version": 3,
                "base_plane_version": "1.4.2",
                "capabilities": [
                    "workspaces",
                    "workspace_members",
                    "pages",
                    "page_versions",
                    "webhooks",
                    "estimates",
                    "work_item_archive",
                    "work_item_relation_delete",
                    "work_item_relation_ids",
                    "generic_asset_delete",
                    "agent_registrations",
                ],
                "page_content": {
                    "input": "description_html",
                    "update_method": "PATCH",
                    "live_required": True,
                    "revision_header": "If-Match",
                },
                "native_releases": False,
            }
        )
