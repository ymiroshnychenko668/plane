"""One inventory service for browser sessions and API keys, with explicit sharing."""

import hashlib
import json
import re
from datetime import timezone as datetime_timezone
from uuid import UUID

from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from plane.app.serializers.infrastructure import (
    KEY,
    SLUG,
    ProjectLinkInput,
    RelationInput,
    ResourceInput,
    safe_text,
)
from plane.app.views.base import BaseViewSet
from plane.db.models import (
    InfrastructureProjectLink,
    InfrastructureRelation,
    InfrastructureResource,
    ProjectMember,
    Workspace,
    WorkspaceMember,
)
from plane.db.models.infrastructure import LIFECYCLES, RELATION_KINDS, RESOURCE_KINDS


class InfrastructureError(APIException):
    def __init__(self, status_code, code, detail):
        self.status_code = status_code
        self.infrastructure_code = code
        super().__init__(detail=detail, code=code)


def fail(status_code, code, detail):
    raise InfrastructureError(status_code, code, detail)


def expected_revision(request, current):
    header = request.headers.get("If-Match")
    if header is None:
        fail(428, "precondition_required", "If-Match is required.")
    match = re.fullmatch(r'(?:W/)?"(0|[1-9][0-9]{0,18})"', header.strip())
    if not match:
        fail(400, "validation_error", "Expected one quoted numeric revision.")
    if int(match.group(1)) != current:
        fail(412, "revision_conflict", "The revision changed. Read the current record before retrying.")


def timestamp(value):
    return value.astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z") if value else None


class InfrastructurePermission(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_anonymous:
            return False
        member = view.memberships().filter(project_id=view.kwargs["project_id"]).select_related("project").first()
        if member is None:
            fail(403, "forbidden", "An active readable project membership is required.")
        view._membership = member
        return True


class InfrastructureViewSet(BaseViewSet):
    permission_classes = [InfrastructurePermission]
    use_read_replica = False
    serializer_class = ResourceInput
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def dispatch(self, request, *args, **kwargs):
        # BaseViewSet's DEBUG tracing prints the full query string. Inventory
        # must never log rejected credential-shaped input, even in development.
        return APIView.dispatch(self, request, *args, **kwargs)

    def memberships(self):
        workspace_ids = WorkspaceMember.objects.filter(
            member=self.request.user,
            is_active=True,
            workspace__slug=self.kwargs["slug"],
            workspace__deleted_at__isnull=True,
        ).values("workspace_id")
        return ProjectMember.objects.filter(
            member=self.request.user,
            is_active=True,
            workspace_id__in=workspace_ids,
            project__workspace_id__in=workspace_ids,
            project__deleted_at__isnull=True,
        ).filter(Q(role__in=(15, 20)) | Q(role=5, project__guest_view_all_features=True))

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, InfrastructureError):
            return Response(
                {"error": {"code": exc.infrastructure_code, "detail": str(exc.detail), "fields": {}}},
                status=exc.status_code,
            )
        if isinstance(exc, serializers.ValidationError):
            return Response(
                {"error": {"code": "validation_error", "detail": "Invalid inventory metadata.", "fields": exc.detail}},
                status=400,
            )
        if isinstance(exc, (NotFound, PermissionDenied)):
            code = "not_found" if isinstance(exc, NotFound) else "forbidden"
            return Response(
                {
                    "error": {
                        "code": code,
                        "detail": "Resource unavailable." if code == "not_found" else "Operation not permitted.",
                        "fields": {},
                    }
                },
                status=exc.status_code,
            )
        if isinstance(exc, IntegrityError):
            return Response(
                {
                    "error": {
                        "code": "conflict",
                        "detail": "Inventory record conflicts with an existing record.",
                        "fields": {},
                    }
                },
                status=409,
            )
        return super().handle_exception(exc)

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        allowed = {
            "list": {"kind", "key", "lifecycle", "tags", "search", "per_page", "cursor"},
            "relations_list": {"kind", "source_id", "target_id", "resource_id", "per_page", "cursor"},
            "links_list": {"per_page", "cursor"},
        }.get(self.action, set())
        if set(request.query_params) - allowed or any(
            len(request.query_params.getlist(key)) != 1 for key in request.query_params
        ):
            fail(400, "validation_error", "Unknown or repeated query parameters are not allowed.")

    def lock_workspace(self):
        # A workspace lock serializes natural keys, sharing and graph checks. It
        # does not confer membership or allow a caller to choose another actor.
        Workspace.objects.select_for_update().get(id=self._membership.workspace_id)
        InfrastructurePermission().has_permission(self.request, self)

    def require_admin(self):
        if self._membership.role != 20:
            fail(403, "forbidden", "Project administrator membership is required.")

    def resources(self):
        return InfrastructureResource.objects.filter(
            workspace_id=self._membership.workspace_id,
            ownership_project__deleted_at__isnull=True,
            project_links__project_id=self.kwargs["project_id"],
            project_links__deleted_at__isnull=True,
        )

    def resource(self, resource_id):
        resource = self.resources().filter(id=resource_id).first()
        if resource is None:
            fail(404, "not_found", "Resource unavailable.")
        return resource

    def can_manage(self, resource):
        return self._membership.role == 20 and resource.ownership_project_id == self._membership.project_id

    def require_owner(self, resource):
        if not self.can_manage(resource):
            fail(403, "forbidden", "The ownership project's administrator must manage this resource.")

    def resource_data(self, resource):
        if not hasattr(self, "_visible_project_ids"):
            self._visible_project_ids = set(self.memberships().values_list("project_id", flat=True))
        fields = (
            "key",
            "name",
            "kind",
            "lifecycle",
            "description",
            "tags",
            "owner",
            "source",
            "evidence",
            "access_methods",
            "revision",
        )
        return {
            "id": str(resource.id),
            **{key: getattr(resource, key) for key in fields},
            "last_verified": timestamp(resource.last_verified),
            "created_at": timestamp(resource.created_at),
            "updated_at": timestamp(resource.updated_at),
            "ownership_project_id": str(resource.ownership_project_id)
            if resource.ownership_project_id in self._visible_project_ids
            else None,
            "can_manage": self.can_manage(resource),
            "can_detach": self._membership.role == 20 and resource.ownership_project_id != self._membership.project_id,
        }

    def resource_response(self, resource, status=200):
        return Response(self.resource_data(resource), status=status, headers={"ETag": f'"{resource.revision}"'})

    def page(self, queryset, render, filters):
        raw_size = self.request.query_params.get("per_page", "50")
        if not re.fullmatch(r"[1-9][0-9]{0,2}", raw_size) or int(raw_size) > 100:
            fail(400, "validation_error", "per_page must be an integer from 1 to 100.")
        size = int(raw_size)
        context = {
            "actor": str(self.request.user.id),
            "workspace": str(self._membership.workspace_id),
            "project": str(self._membership.project_id),
            "action": self.action,
            "resource": str(self.kwargs.get("resource_id", "")),
            "filters": filters,
            "per_page": size,
        }
        digest = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
        cursor = self.request.query_params.get("cursor")
        if cursor is not None:
            try:
                if len(cursor) > 2048:
                    raise ValueError()
                payload = signing.loads(cursor, salt="infrastructure-v1")
                if (
                    not isinstance(payload, dict)
                    or set(payload) != {"context", "after"}
                    or payload["context"] != digest
                ):
                    raise ValueError()
                after = UUID(payload["after"])
            except (signing.BadSignature, ValueError, TypeError, KeyError):
                fail(400, "invalid_cursor", "Invalid cursor for this request.")
            queryset = queryset.filter(id__gt=after)
        rows = list(queryset.order_by("id")[: size + 1])
        more = len(rows) > size
        rows = rows[:size]
        next_cursor = (
            signing.dumps({"context": digest, "after": str(rows[-1].id)}, salt="infrastructure-v1", compress=True)
            if more
            else None
        )
        return Response(
            {"results": [render(row) for row in rows], "next_cursor": next_cursor, "has_more": more, "per_page": size}
        )

    def list(self, request, slug, project_id):
        queryset = self.resources()
        filters = {}
        for key, choices in (("kind", RESOURCE_KINDS), ("lifecycle", LIFECYCLES)):
            if key in request.query_params:
                value = request.query_params[key]
                if value not in choices:
                    fail(400, "validation_error", "Unknown resource filter choice.")
                queryset = queryset.filter(**{key: value})
                filters[key] = value
        if "key" in request.query_params:
            value = request.query_params["key"]
            if len(value) > 160 or not re.fullmatch(KEY, value):
                fail(400, "validation_error", "Invalid resource key filter.")
            queryset = queryset.filter(key=value)
            filters["key"] = value
        if "tags" in request.query_params:
            tags = request.query_params["tags"].split(",")
            if len(tags) > 20 or any(len(value) > 64 or not re.fullmatch(SLUG, value) for value in tags):
                fail(400, "validation_error", "Invalid tag filter.")
            filters["tags"] = sorted(set(tags))
            queryset = queryset.filter(tags__contains=filters["tags"])
        if "search" in request.query_params:
            value = safe_text(request.query_params["search"]).strip()
            if len(value) > 200:
                fail(400, "validation_error", "Search must not exceed 200 characters.")
            filters["search"] = value.casefold()
            queryset = queryset.filter(Q(name__icontains=value) | Q(key__icontains=value))
        return self.page(queryset, self.resource_data, filters)

    def retrieve(self, request, slug, project_id, resource_id):
        return self.resource_response(self.resource(resource_id))

    @transaction.atomic
    def create(self, request, slug, project_id):
        self.lock_workspace()
        self.require_admin()
        expected_revision(request, 0)
        serializer = ResourceInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        if InfrastructureResource.all_objects.filter(
            workspace_id=self._membership.workspace_id, key=data["key"]
        ).exists():
            fail(409, "conflict", "Inventory record conflicts with an existing record.")
        resource = InfrastructureResource(
            workspace_id=self._membership.workspace_id,
            ownership_project_id=project_id,
            created_by=request.user,
            **data,
        )
        resource.save(disable_auto_set_user=True)
        link = InfrastructureProjectLink(resource=resource, project_id=project_id, created_by=request.user)
        link.save(disable_auto_set_user=True)
        return self.resource_response(resource, 201)

    @transaction.atomic
    def partial_update(self, request, slug, project_id, resource_id):
        self.lock_workspace()
        resource = self.resource(resource_id)
        self.require_owner(resource)
        expected_revision(request, resource.revision)
        if not isinstance(request.data, dict):
            fail(400, "validation_error", "Expected an object.")
        previous = {key: getattr(resource, key) for key in ResourceInput().fields}
        previous["last_verified"] = timestamp(resource.last_verified)
        serializer = ResourceInput(data={**previous, **request.data})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        for key in ("key", "kind"):
            if data[key] != getattr(resource, key):
                fail(400, "validation_error", "Resource key and kind are immutable.")
        if any(getattr(resource, key) != value for key, value in data.items()):
            for key, value in data.items():
                setattr(resource, key, value)
            self.touch(resource)
        return self.resource_response(resource)

    def touch(self, resource):
        resource.revision += 1
        resource.updated_by = self.request.user
        resource.save(disable_auto_set_user=True)

    @transaction.atomic
    def destroy(self, request, slug, project_id, resource_id):
        self.lock_workspace()
        resource = self.resource(resource_id)
        self.require_owner(resource)
        expected_revision(request, resource.revision)
        if (
            InfrastructureProjectLink.objects.filter(resource=resource, project__deleted_at__isnull=True)
            .exclude(project_id=project_id)
            .exists()
        ):
            fail(409, "conflict", "Detach other project links before deleting this resource.")
        InfrastructureRelation.objects.filter(Q(source_resource=resource) | Q(target_resource=resource)).delete()
        InfrastructureProjectLink.objects.filter(resource=resource).delete()
        resource.deleted_at = timezone.now()
        self.touch(resource)
        return Response(status=204)

    def relations(self):
        visible = self.resources().values("id")
        return InfrastructureRelation.objects.filter(
            project_id=self._membership.project_id,
            source_resource_id__in=visible,
            target_resource_id__in=visible,
        ).select_related("source_resource", "target_resource")

    def relation_data(self, relation):
        return {
            "id": str(relation.id),
            "project_id": str(relation.project_id),
            "source_id": str(relation.source_resource_id),
            "target_id": str(relation.target_resource_id),
            "source_name": relation.source_resource.name,
            "source_key": relation.source_resource.key,
            "target_name": relation.target_resource.name,
            "target_key": relation.target_resource.key,
            "kind": relation.kind,
            "source": relation.source,
            "evidence": relation.evidence,
            "last_verified": timestamp(relation.last_verified),
            "created_at": timestamp(relation.created_at),
            "revision": relation.revision,
            "can_manage": self._membership.role == 20,
        }

    def relations_list(self, request, slug, project_id):
        queryset = self.relations()
        filters = {}
        if "kind" in request.query_params:
            kind = request.query_params["kind"]
            if kind not in RELATION_KINDS:
                fail(400, "validation_error", "Unknown relation kind.")
            queryset = queryset.filter(kind=kind)
            filters["kind"] = kind
        for field in ("source_id", "target_id", "resource_id"):
            if field in request.query_params:
                try:
                    value = UUID(request.query_params[field])
                except ValueError:
                    fail(400, "validation_error", "Expected a UUID relation filter.")
                filters[field] = str(value)
                if field == "resource_id":
                    queryset = queryset.filter(Q(source_resource_id=value) | Q(target_resource_id=value))
                else:
                    queryset = queryset.filter(**{field.replace("_id", "_resource_id"): value})
        return self.page(queryset, self.relation_data, filters)

    @transaction.atomic
    def relations_create(self, request, slug, project_id):
        self.lock_workspace()
        self.require_admin()
        expected_revision(request, 0)
        serializer = RelationInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        source = self.resource(data.pop("source_id"))
        target = self.resource(data.pop("target_id"))
        if source.id == target.id or source.lifecycle == "retired" or target.lifecycle == "retired":
            fail(400, "validation_error", "Relations require distinct, non-retired resources.")
        kind = data["kind"]
        if kind == "runs_on" and (
            source.kind not in {"vm", "lxc", "service", "database", "storage"}
            or target.kind not in {"physical_host", "vm", "lxc", "service", "storage", "network"}
            or (source.kind in {"vm", "lxc"} and target.kind not in {"physical_host", "vm"})
        ):
            fail(400, "validation_error", "These resource kinds cannot form a runs_on relation.")
        relations = self.relations().filter(kind=kind)
        if relations.filter(source_resource=source, target_resource=target).exists():
            fail(409, "conflict", "This relation already exists.")
        adjacency = {}
        for left, right in relations.values_list("source_resource_id", "target_resource_id"):
            adjacency.setdefault(left, []).append(right)
        pending, visited = [target.id], set()
        while pending:
            node = pending.pop()
            if node == source.id:
                fail(400, "validation_error", "Relations of the same kind must not form a cycle.")
            if node not in visited:
                visited.add(node)
                pending.extend(adjacency.get(node, []))
        relation = InfrastructureRelation(
            project_id=project_id,
            source_resource=source,
            target_resource=target,
            created_by=request.user,
            **data,
        )
        relation.save(disable_auto_set_user=True)
        return Response(self.relation_data(relation), status=201, headers={"ETag": '"1"'})

    @transaction.atomic
    def relations_delete(self, request, slug, project_id, relation_id):
        self.lock_workspace()
        self.require_admin()
        relation = self.relations().filter(id=relation_id).first()
        if relation is None:
            fail(404, "not_found", "Relation unavailable.")
        expected_revision(request, relation.revision)
        InfrastructureRelation.objects.filter(id=relation.id).delete()
        return Response(status=204)

    def links_list(self, request, slug, project_id, resource_id):
        resource = self.resource(resource_id)
        queryset = InfrastructureProjectLink.objects.filter(
            resource=resource, project_id__in=self.memberships().values("project_id")
        ).select_related("project")
        return self.page(
            queryset,
            lambda link: {
                "id": str(link.id),
                "resource_id": str(link.resource_id),
                "project_id": str(link.project_id),
                "project_name": link.project.name,
                "kind": "serves_project",
                "is_owner": link.project_id == resource.ownership_project_id,
                "revision": resource.revision,
            },
            {},
        )

    def target_admin(self, project_id):
        member = self.memberships().filter(project_id=project_id).first()
        if member is None:
            fail(404, "not_found", "Project unavailable.")
        if member.role != 20:
            fail(403, "forbidden", "Target project administrator membership is required.")

    @transaction.atomic
    def links_attach(self, request, slug, project_id, resource_id):
        self.lock_workspace()
        resource = self.resource(resource_id)
        self.require_owner(resource)
        serializer = ProjectLinkInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        target_id = serializer.validated_data["project_id"]
        self.target_admin(target_id)
        expected_revision(request, resource.revision)
        if not InfrastructureProjectLink.objects.filter(resource=resource, project_id=target_id).exists():
            link = InfrastructureProjectLink(resource=resource, project_id=target_id, created_by=request.user)
            link.save(disable_auto_set_user=True)
            self.touch(resource)
        return self.resource_response(resource)

    @transaction.atomic
    def links_detach(self, request, slug, project_id, resource_id, target_project_id):
        self.lock_workspace()
        resource = self.resource(resource_id)
        self.require_admin()
        if self._membership.project_id != target_project_id:
            self.require_owner(resource)
        self.target_admin(target_project_id)
        expected_revision(request, resource.revision)
        if target_project_id == resource.ownership_project_id:
            fail(409, "conflict", "The ownership project link cannot be detached.")
        link = InfrastructureProjectLink.objects.filter(resource=resource, project_id=target_project_id).first()
        if link is None:
            fail(404, "not_found", "Project link unavailable.")
        InfrastructureRelation.objects.filter(project_id=target_project_id).filter(
            Q(source_resource=resource) | Q(target_resource=resource)
        ).delete()
        InfrastructureProjectLink.objects.filter(id=link.id).delete()
        self.touch(resource)
        return Response(status=204, headers={"ETag": f'"{resource.revision}"'})
