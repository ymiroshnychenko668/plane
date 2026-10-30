"""Project-scoped dispatch routes, public service grants and execution ownership."""

import base64
import binascii
import json

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from plane.api.views.agent_registration import (
    IdentifierField,
    RegistrationRevisionConflict,
    StrictCharField,
    StrictSerializer,
    expected_revision,
)
from plane.api.views.base import BaseAPIView
from plane.api.views.management import APIKeyManagementMixin
from plane.db.models import (
    Issue,
    IssueAssignee,
    Project,
    ProjectAgentRegistration,
    ProjectAssignmentRoute,
    ProjectDispatchServiceGrant,
    ProjectMember,
    ProjectTeamContact,
    WorkItemExecution,
    WorkItemExecutionClaim,
    WorkspaceMember,
)
from plane.utils.team import TeamConflict, address_value, available, member_active, require_migrated


class AssignmentPermission(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_anonymous:
            return False
        membership = ProjectMember.objects.filter(
            project_id=view.kwargs["project_id"],
            workspace__slug=view.kwargs["slug"],
            member=request.user,
            member__is_active=True,
            is_active=True,
        ).first()
        if (
            not membership
            or not WorkspaceMember.objects.filter(
                workspace_id=membership.workspace_id, member=request.user, is_active=True
            ).exists()
        ):
            return False
        view._request_membership = membership
        return request.method in {"GET", "HEAD", "OPTIONS"} or membership.role >= 15


class AssignmentBase(APIKeyManagementMixin, BaseAPIView):
    permission_classes = [AssignmentPermission]

    def project(self, lock=False):
        queryset = Project.objects.select_for_update(of=("self",)) if lock else Project.objects.all()
        return get_object_or_404(queryset, id=self.kwargs["project_id"], workspace__slug=self.kwargs["slug"])

    def require_admin(self):
        if self._request_membership.role != 20:
            raise PermissionDenied("Only a project admin may configure assignment delivery.")

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class BooleanInput(StrictSerializer):
    active = serializers.BooleanField(default=True)

    def validate_active(self, value):
        if type(self.initial_data.get("active", True)) is not bool:
            raise serializers.ValidationError("Expected a boolean.")
        return value


class RouteInput(BooleanInput):
    contact_id = serializers.UUIDField()


def route_data(route):
    contact = route.contact
    actor = contact.registration
    return {
        "id": str(route.id),
        "project_id": str(route.project_id),
        "user_id": str(route.user_id),
        "contact_id": str(route.contact_id),
        "address": contact.address,
        "registration_id": str(actor.id),
        "runtime": actor.runtime,
        "target": contact.target,
        "active": route.active,
        "available": bool(route.active and actor.user_id == route.user_id and available(contact)),
        "revision": route.revision,
    }


class AssignmentRouteAPIEndpoint(AssignmentBase):
    http_method_names = ["get", "put", "delete", "head", "options"]

    def get(self, request, slug, project_id, user_id=None):
        project = self.project()
        require_migrated(project)
        routes = ProjectAssignmentRoute.objects.filter(project=project).select_related(
            "contact__registration", "contact__project"
        )
        if user_id is None:
            return Response([route_data(route) for route in routes])
        route = get_object_or_404(routes, user_id=user_id)
        return Response(route_data(route), headers={"ETag": f'"{route.revision}"'})

    @transaction.atomic
    def put(self, request, slug, project_id, user_id=None):
        if user_id is None:
            return Response(status=405)
        self.require_admin()
        expected = expected_revision(request)
        serializer = RouteInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        project = self.project(lock=True)
        require_migrated(project)
        contact = get_object_or_404(
            ProjectTeamContact.objects.select_related("registration", "project"),
            project=project,
            id=data["contact_id"],
        )
        if str(contact.registration.user_id) != str(user_id):
            raise TeamConflict("The assignment route must use a contact belonging to its assignee.")
        if data["active"] and not available(contact):
            raise TeamConflict("An active assignment route requires an available registered contact.")
        route = ProjectAssignmentRoute.objects.filter(project=project, user_id=user_id).first()
        if expected != (route.revision if route else 0):
            raise RegistrationRevisionConflict()
        creating = route is None
        if creating:
            route = ProjectAssignmentRoute(project=project, user_id=user_id, contact=contact)
        changed = creating or route.contact_id != contact.id or route.active != data["active"]
        route.contact, route.active = contact, data["active"]
        if changed:
            if not creating:
                route.revision += 1
            route.save()
        return Response(route_data(route), status=201 if creating else 200, headers={"ETag": f'"{route.revision}"'})

    @transaction.atomic
    def delete(self, request, slug, project_id, user_id=None):
        if user_id is None:
            return Response(status=405)
        self.require_admin()
        expected = expected_revision(request)
        project = self.project(lock=True)
        require_migrated(project)
        route = get_object_or_404(ProjectAssignmentRoute, project=project, user_id=user_id)
        if route.revision != expected:
            raise RegistrationRevisionConflict()
        if route.active:
            route.active = False
            route.revision += 1
            route.save(update_fields=("active", "revision", "updated_at"))
        return Response(route_data(route), headers={"ETag": f'"{route.revision}"'})


class GrantInput(BooleanInput):
    user_id = serializers.UUIDField()
    sender_address = StrictCharField(max_length=128)
    public_key = StrictCharField(max_length=44, min_length=44)

    def validate_sender_address(self, value):
        return address_value(value)

    def validate_public_key(self, value):
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error):
            raise serializers.ValidationError("Expected a base64-encoded raw Ed25519 public key.") from None
        if len(raw) != 32 or base64.b64encode(raw).decode("ascii") != value:
            raise serializers.ValidationError("Expected a base64-encoded raw Ed25519 public key.")
        return value


def grant_data(grant):
    return {
        "id": str(grant.id),
        "project_id": str(grant.project_id),
        "user_id": str(grant.user_id),
        "sender_address": grant.sender_address,
        "public_key": grant.public_key,
        "active": grant.active,
        "available": bool(grant.active and member_active(grant.project, grant.user_id)),
        "revision": grant.revision,
    }


class DispatchServiceGrantAPIEndpoint(AssignmentBase):
    http_method_names = ["get", "post", "put", "head", "options"]

    def get(self, request, slug, project_id, grant_id=None):
        project = self.project()
        require_migrated(project)
        grants = ProjectDispatchServiceGrant.objects.filter(project=project).select_related("project")
        if grant_id is None:
            return Response([grant_data(grant) for grant in grants])
        grant = get_object_or_404(grants, id=grant_id)
        return Response(grant_data(grant), headers={"ETag": f'"{grant.revision}"'})

    @transaction.atomic
    def post(self, request, slug, project_id, grant_id=None):
        if grant_id is not None:
            return Response(status=405)
        return self.write(request, creating=True)

    @transaction.atomic
    def put(self, request, slug, project_id, grant_id=None):
        if grant_id is None:
            return Response(status=405)
        return self.write(request, grant_id=grant_id)

    def write(self, request, grant_id=None, creating=False):
        self.require_admin()
        expected = expected_revision(request)
        serializer = GrantInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        project = self.project(lock=True)
        require_migrated(project)
        if data["active"] and not member_active(project, data["user_id"]):
            raise TeamConflict("An active dispatch service grant requires an active project and workspace member.")
        # A service sender is a separate, explicit principal, never a second
        # owner of an existing agent address.
        if ProjectTeamContact.objects.filter(project=project, address=data["sender_address"]).exists():
            raise TeamConflict("A dispatch service sender cannot reuse a team contact's address.")
        if creating:
            if expected != 0:
                raise RegistrationRevisionConflict()
            if ProjectDispatchServiceGrant.objects.filter(
                project=project, sender_address=data["sender_address"]
            ).exists():
                raise TeamConflict("This service sender already has a grant; read and update it conditionally.")
            grant = ProjectDispatchServiceGrant.objects.create(project=project, **data)
        else:
            grant = get_object_or_404(ProjectDispatchServiceGrant, project=project, id=grant_id)
            if expected != grant.revision:
                raise RegistrationRevisionConflict()
            if str(data["user_id"]) != str(grant.user_id) or data["sender_address"] != grant.sender_address:
                raise TeamConflict("A grant's principal and sender address are immutable; create a separate grant.")
            if any(getattr(grant, key) != value for key, value in data.items()):
                for key, value in data.items():
                    setattr(grant, key, value)
                grant.revision += 1
                grant.save()
        return Response(grant_data(grant), status=201 if creating else 200, headers={"ETag": f'"{grant.revision}"'})


class ClaimInput(StrictSerializer):
    registration_id = serializers.UUIDField()
    run_id = IdentifierField()
    request_id = IdentifierField()


class CheckpointInput(StrictSerializer):
    registration_id = serializers.UUIDField()
    run_id = IdentifierField()
    checkpoint = serializers.JSONField()
    status = serializers.ChoiceField(choices=("running", "awaiting", "paused", "completed", "failed", "stopped"))

    def validate_checkpoint(self, value):
        if not isinstance(value, dict) or len(json.dumps(value, ensure_ascii=False).encode("utf-8")) > 65536:
            raise serializers.ValidationError("Expected a checkpoint object of at most 64 KiB, without credentials.")
        return value


def execution_stop_reasons(issue, execution):
    if not execution or not execution.registration_id:
        return []
    actor = execution.registration
    reasons = []
    if not IssueAssignee.objects.filter(issue=issue, assignee_id=actor.user_id).exists():
        reasons.append("unassigned")
    if not actor.active or not member_active(issue.project, actor.user_id):
        reasons.append("identity_unavailable")
    route = (
        ProjectAssignmentRoute.objects.filter(project=issue.project, user_id=actor.user_id)
        .select_related("contact__registration", "contact__project")
        .first()
    )
    if not route or not route_data(route)["available"]:
        reasons.append("route_unavailable")
    elif route.contact.registration_id != actor.id:
        reasons.append("route_changed")
    if issue.archived_at or issue.project.archived_at:
        reasons.append("archived")
    if issue.state and issue.state.group in {"completed", "cancelled"}:
        reasons.append(issue.state.group)
    if issue.is_draft:
        reasons.append("draft")
    return reasons


def execution_data(issue, execution):
    stop_reasons = execution_stop_reasons(issue, execution)
    actor = execution.registration if execution and execution.registration_id else None
    return {
        "work_item_id": str(issue.id),
        "registration_id": str(actor.id) if actor else None,
        "user_id": str(actor.user_id) if actor else None,
        "run_id": execution.run_id if actor else None,
        "request_id": execution.request_id if actor else None,
        "status": execution.status if execution else "idle",
        "checkpoint": execution.checkpoint if execution else {},
        "revision": execution.revision if execution else 0,
        "stop_requested": bool(stop_reasons),
        "stop_reasons": stop_reasons,
    }


class WorkItemExecutionAPIEndpoint(AssignmentBase):
    http_method_names = ["get", "post", "head", "options"]

    def issue(self, lock=False):
        queryset = Issue.objects.select_related("project", "state")
        if lock:
            queryset = queryset.select_for_update(of=("self",))
        return get_object_or_404(
            queryset,
            id=self.kwargs["work_item_id"],
            project_id=self.kwargs["project_id"],
            workspace__slug=self.kwargs["slug"],
        )

    def response(self, issue, execution, status=200):
        data = execution_data(issue, execution)
        return Response(data, status=status, headers={"ETag": f'"{data["revision"]}"'})

    def get(self, request, slug, project_id, work_item_id, operation=None):
        if operation:
            return Response(status=405)
        issue = self.issue()
        execution = WorkItemExecution.objects.filter(issue=issue).select_related("registration").first()
        return self.response(issue, execution)

    @transaction.atomic
    def post(self, request, slug, project_id, work_item_id, operation=None):
        if operation not in {"claim", "checkpoint", "release"}:
            return Response(status=405)
        serializer = ClaimInput(data=request.data) if operation == "claim" else CheckpointInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        issue = self.issue(lock=True)
        actor = get_object_or_404(ProjectAgentRegistration, project=issue.project, id=data["registration_id"])
        if actor.user_id != request.user.id:
            raise PermissionDenied("Only the registered agent's own principal may manage its execution.")
        execution = WorkItemExecution.objects.filter(issue=issue).select_related("registration").first()
        if operation == "claim":
            if execution and execution.registration_id:
                if (
                    execution.registration_id == actor.id
                    and execution.run_id == data["run_id"]
                    and execution.request_id == data["request_id"]
                ):
                    return self.response(issue, execution)
                raise TeamConflict(
                    "This work item already has an execution owner; stop and release it before takeover."
                )
            if not actor.active or not member_active(issue.project, actor.user_id):
                raise TeamConflict("The agent identity is not active in this project.")
            if not IssueAssignee.objects.filter(issue=issue, assignee_id=actor.user_id).exists():
                raise TeamConflict("Only an assigned agent may claim this work item.")
            route = (
                ProjectAssignmentRoute.objects.filter(project=issue.project, user_id=actor.user_id)
                .select_related("contact__registration", "contact__project")
                .first()
            )
            if not route or not route_data(route)["available"] or route.contact.registration_id != actor.id:
                raise TeamConflict("Only the assignee's available primary receiver may claim this work item.")
            if (
                issue.archived_at
                or issue.project.archived_at
                or issue.is_draft
                or (issue.state and issue.state.group in {"completed", "cancelled"})
            ):
                raise TeamConflict("Archived, draft, completed or cancelled work items cannot be claimed.")
            if (
                WorkItemExecutionClaim.objects.filter(issue=issue)
                .filter(Q(run_id=data["run_id"]) | Q(request_id=data["request_id"]))
                .exists()
            ):
                raise TeamConflict("This run has already released ownership; resume with a new run and request ID.")
            creating = execution is None
            if creating:
                execution = WorkItemExecution(issue=issue)
            else:
                execution.revision += 1
            execution.registration = actor
            execution.run_id, execution.request_id = data["run_id"], data["request_id"]
            execution.status = "running"
            WorkItemExecutionClaim.objects.create(
                issue=issue, registration=actor, run_id=data["run_id"], request_id=data["request_id"]
            )
            execution.save()
            return self.response(issue, execution, status=201 if creating else 200)
        expected = expected_revision(request)
        if not execution or execution.registration_id != actor.id or execution.run_id != data["run_id"]:
            raise TeamConflict("Only the current owner and run may checkpoint or release this execution.")
        if execution.revision != expected:
            raise RegistrationRevisionConflict()
        allowed = (
            {"paused", "completed", "failed", "stopped"}
            if operation == "release"
            else {"running", "awaiting", "paused"}
        )
        if data["status"] not in allowed:
            raise serializers.ValidationError({"status": "This status is not valid for the requested operation."})
        changed = (
            execution.checkpoint != data["checkpoint"] or execution.status != data["status"] or operation == "release"
        )
        if changed:
            execution.checkpoint, execution.status = data["checkpoint"], data["status"]
            if operation == "release":
                execution.registration = None
            execution.revision += 1
            execution.save()
        return self.response(issue, execution)
