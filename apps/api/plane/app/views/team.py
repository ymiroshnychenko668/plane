"""One Teams service for browser sessions and workspace-scoped API keys."""

import hashlib
import json

from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import serializers
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from plane.api.views.agent_registration import (
    BindingSerializer,
    RegistrationReadSerializer,
    RegistrationRevisionConflict,
    StrictSerializer,
    StrictCharField,
    expected_revision,
    validate_slot,
)
from plane.app.views.base import BaseViewSet
from plane.db.models import (
    Page,
    Project,
    ProjectAgentRegistration,
    ProjectMember,
    ProjectTeamContact,
    ProjectTeamMigration,
    ProjectUserProperty,
    WorkspaceMember,
)
from plane.utils.team import (
    TeamConflict,
    active_member_ids,
    address_value,
    available,
    legacy_pages,
    legacy_status,
    member_active,
    parse_document,
    require_migrated,
    target_matches,
    target_value,
)


class TeamPermission(BasePermission):
    def has_permission(self, request, view):
        if request.user.is_anonymous:
            return False
        member = ProjectMember.objects.filter(
            project_id=view.kwargs["project_id"],
            workspace__slug=view.kwargs["slug"],
            member=request.user,
            is_active=True,
        ).first()
        if (
            not member
            or not WorkspaceMember.objects.filter(
                workspace_id=member.workspace_id, member=request.user, is_active=True
            ).exists()
        ):
            return False
        view._request_membership = member
        return request.method in {"GET", "HEAD", "OPTIONS"} or view.action == "resolve" or member.role >= 15


class ContactInput(StrictSerializer):
    registration_id = serializers.UUIDField()
    target = serializers.JSONField()
    active = serializers.BooleanField(default=True)

    def validate_active(self, value):
        if type(self.initial_data.get("active", True)) is not bool:
            raise serializers.ValidationError("Expected a boolean.")
        return value


class AgentInput(StrictSerializer):
    display_name = StrictCharField(max_length=255, required=False)
    role = StrictCharField(max_length=4096, required=False)
    active = serializers.BooleanField(required=False)

    def validate_active(self, value):
        if type(self.initial_data["active"]) is not bool:
            raise serializers.ValidationError("Expected a boolean.")
        return value


class MemberInput(StrictSerializer):
    role = serializers.ChoiceField(choices=(5, 15, 20))


class AgentCreateInput(StrictSerializer):
    runtime = serializers.ChoiceField(choices=("codex", "hermes"))
    agent_key = StrictCharField(max_length=64)
    display_name = StrictCharField(max_length=255)
    role = StrictCharField(max_length=4096)
    user_id = serializers.UUIDField()
    binding = serializers.JSONField()

    def validate(self, data):
        validate_slot(data["runtime"], data["agent_key"])
        if data["agent_key"].startswith("chat-"):
            raise serializers.ValidationError(
                {"agent_key": "Join a real chat through the existing chat-agents endpoint."}
            )
        binding = BindingSerializer(data=data["binding"], context={"runtime": data["runtime"]})
        binding.is_valid(raise_exception=True)
        data["binding"] = binding.validated_data
        target = {
            "conversation_id" if key == "session_id" else key: value
            for key, value in data["binding"].items()
            if key in {"thread_id", "host_id", "agent_id", "session_id", "profile"}
        }
        target_value(data["runtime"], target)
        return data


def member_data(member):
    return {
        "id": str(member.member_id),
        "membership_id": str(member.id),
        "display_name": member.member.display_name,
        "role": member.role,
        "is_active": member.is_active,
        "is_bot": member.member.is_bot,
    }


class TeamViewSet(BaseViewSet):
    permission_classes = [TeamPermission]
    use_read_replica = False

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def project(self, lock=False):
        queryset = Project.objects.select_for_update() if lock else Project.objects.all()
        return get_object_or_404(queryset, id=self.kwargs["project_id"], workspace__slug=self.kwargs["slug"])

    def admin(self, project):
        return self._request_membership.project_id == project.id and self._request_membership.role == 20

    def workspace_admin(self, project):
        return WorkspaceMember.objects.filter(
            workspace_id=project.workspace_id, member=self.request.user, is_active=True, role=20
        ).exists()

    def can_manage(self, actor):
        return self.admin(actor.project) or (
            str(actor.user_id) == str(self.request.user.id)
            and self._request_membership.project_id == actor.project_id
            and self._request_membership.role >= 15
        )

    def require_actor_manager(self, actor):
        if not self.can_manage(actor):
            raise PermissionDenied("Only this identity's owner or a project admin can manage its registry entry.")

    def actor_data(self, actor):
        data = dict(RegistrationReadSerializer(actor).data)
        data.update(
            registration_id=str(actor.id),
            member=(
                actor.user_id in self._active_users
                if hasattr(self, "_active_users")
                else member_active(actor.project, actor.user_id)
            ),
            can_manage=self.can_manage(actor),
        )
        return data

    def contact_data(self, contact):
        actor = contact.registration
        return {
            "id": str(contact.id),
            "address": contact.address,
            "registration_id": str(actor.id),
            "runtime": actor.runtime,
            "agent_key": actor.agent_key,
            "user_id": str(actor.user_id) if actor.user_id else None,
            "target": contact.target,
            "active": contact.active,
            "available": available(contact, getattr(self, "_active_users", None)),
            "revision": contact.revision,
            "display_name": actor.display_name,
            "role": actor.provisioning.get("role"),
            "can_manage": self.can_manage(actor),
        }

    def contact_response(self, contact, status=200):
        return Response(self.contact_data(contact), status=status, headers={"ETag": f'"{contact.revision}"'})

    def roster(self, request, slug, project_id):
        project = self.project()
        self._active_users = active_member_ids(project)
        members = ProjectMember.objects.filter(project=project).select_related("member")
        actors = ProjectAgentRegistration.objects.filter(project=project).select_related("project")
        contacts = ProjectTeamContact.objects.filter(project=project).select_related("registration__project", "project")
        value = {
            "project": {"id": str(project.id), "name": project.name, "identifier": project.identifier},
            "members": [member_data(row) for row in members],
            "actors": [self.actor_data(row) for row in actors],
            "contacts": [self.contact_data(row) for row in contacts],
            "legacy": legacy_status(project),
            "can_manage_members": self.admin(project),
        }
        search = request.query_params.get("search", "").strip().casefold()
        if search:
            for key in ("members", "actors", "contacts"):
                value[key] = [
                    row
                    for row in value[key]
                    if search
                    in " ".join(
                        str(row.get(field) or "")
                        for field in ("display_name", "role", "address", "agent_key", "runtime")
                    ).casefold()
                ]
        revision = hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()
        return Response(value, headers={"ETag": f'"{revision}"'})

    def contact_get(self, request, slug, project_id, address):
        address_value(address)
        return self.contact_response(
            get_object_or_404(
                ProjectTeamContact.objects.select_related("registration", "project"),
                project=self.project(),
                address=address,
            )
        )

    @transaction.atomic
    def contact_put(self, request, slug, project_id, address):
        address_value(address)
        expected = expected_revision(request)
        serializer = ContactInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        project = self.project(lock=True)
        require_migrated(project)
        actor = get_object_or_404(ProjectAgentRegistration, project=project, id=data["registration_id"])
        self.require_actor_manager(actor)
        target_value(actor.runtime, data["target"])
        if data["active"] and (
            not actor.active or not member_active(project, actor.user_id) or not target_matches(actor, data["target"])
        ):
            raise TeamConflict("The contact must target its active project identity's registered receiver.")
        contact = ProjectTeamContact.objects.filter(project=project, address=address).first()
        if contact:
            self.require_actor_manager(contact.registration)
        if expected != (contact.revision if contact else 0):
            raise RegistrationRevisionConflict()
        creating = contact is None
        if creating:
            contact = ProjectTeamContact(project=project, address=address, registration=actor)
        if not creating and contact.registration_id != actor.id:
            # A logical address is a stable slot, never silently assigned to another identity.
            raise TeamConflict("This address belongs to a different registration; choose a new address.")
        changed = creating or contact.target != data["target"] or contact.active != data["active"]
        contact.target, contact.active = data["target"], data["active"]
        if changed:
            if not creating:
                contact.revision += 1
            contact.save()
        return self.contact_response(contact, 201 if creating else 200)

    @transaction.atomic
    def contact_delete(self, request, slug, project_id, address):
        address_value(address)
        expected = expected_revision(request)
        project = self.project(lock=True)
        require_migrated(project)
        contact = get_object_or_404(ProjectTeamContact, project=project, address=address)
        self.require_actor_manager(contact.registration)
        if contact.revision != expected:
            raise RegistrationRevisionConflict()
        if contact.active:
            contact.active = False
            contact.revision += 1
            contact.save(update_fields=("active", "revision", "updated_at"))
        return self.contact_response(contact)

    def resolve(self, request, slug, project_id):
        if not isinstance(request.data, dict) or set(request.data) != {"address"}:
            raise serializers.ValidationError("Provide exactly one address.")
        address = address_value(
            request.data["address"].removeprefix("@")
            if isinstance(request.data["address"], str)
            else request.data["address"]
        )
        contact = get_object_or_404(
            ProjectTeamContact.objects.select_related("registration", "project"),
            project=self.project(),
            address=address,
        )
        if not available(contact):
            raise NotFound("The address has no active, matching project receiver.")
        return self.contact_response(contact)

    @transaction.atomic
    def agent_create(self, request, slug, project_id):
        expected = expected_revision(request)
        project = self.project(lock=True)
        if not self.admin(project):
            raise PermissionDenied("A project admin must add an agent identity.")
        serializer = AgentCreateInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        if not member_active(project, data["user_id"]):
            raise serializers.ValidationError({"user_id": "Select an existing active project and workspace member."})
        if (
            expected != 0
            or ProjectAgentRegistration.objects.filter(
                project=project, runtime=data["runtime"], agent_key=data["agent_key"]
            ).exists()
        ):
            raise RegistrationRevisionConflict()
        role = data.pop("role")
        actor = ProjectAgentRegistration.objects.create(
            project=project, provisioning={"kind": "agent-platform-team-registration", "role": role}, **data
        )
        return Response(self.actor_data(actor), status=201, headers={"ETag": '"1"'})

    @transaction.atomic
    def agent_patch(self, request, slug, project_id, registration_id):
        expected = expected_revision(request)
        serializer = AgentInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        project = self.project(lock=True)
        actor = get_object_or_404(ProjectAgentRegistration, project=project, id=registration_id)
        self.require_actor_manager(actor)
        if expected != actor.revision:
            raise RegistrationRevisionConflict()
        changed = False
        for key, value in serializer.validated_data.items():
            old = actor.provisioning.get("role") if key == "role" else getattr(actor, key)
            changed |= old != value
            if key == "role":
                actor.provisioning = {**actor.provisioning, "role": value}
            else:
                setattr(actor, key, value)
        if changed:
            actor.revision += 1
            actor.save(update_fields=("display_name", "active", "provisioning", "revision", "updated_at"))
        return Response(self.actor_data(actor), headers={"ETag": f'"{actor.revision}"'})

    @transaction.atomic
    def member_write(self, request, slug, project_id, user_id):
        project = self.project(lock=True)
        if not self.admin(project):
            raise PermissionDenied("A project admin must manage membership.")
        workspace_member = get_object_or_404(
            WorkspaceMember,
            workspace_id=project.workspace_id,
            member_id=user_id,
            is_active=True,
            member__is_active=True,
        )
        member = ProjectMember.objects.filter(project=project, member_id=user_id).first()
        removing = request.method == "DELETE"
        if removing and not member:
            raise NotFound()
        if request.method == "PATCH" and not member:
            raise NotFound()
        workspace_admin = self.workspace_admin(project)
        if member and member.role >= 20 and not workspace_admin:
            raise PermissionDenied("A project admin cannot modify another project admin or their own role.")
        if removing:
            if request.data:
                raise serializers.ValidationError("Membership removal takes no body.")
            if member.member_id == request.user.id or member.member_id == project.project_lead_id:
                raise TeamConflict("Transfer project leadership before removing this membership.")
            if (
                member.role == 20
                and ProjectMember.objects.filter(project=project, role=20, is_active=True).count() <= 1
            ):
                raise TeamConflict("A project must retain an active admin.")
            member.is_active = False
            member.save(update_fields=("is_active", "updated_at"))
        else:
            serializer = MemberInput(data=request.data)
            serializer.is_valid(raise_exception=True)
            role = serializer.validated_data["role"]
            if (workspace_member.role == 5 and role > 5) or (workspace_member.role == 20 and role < 20):
                raise serializers.ValidationError({"role": "This role conflicts with the member's workspace role."})
            if role == 20 and not workspace_admin:
                raise PermissionDenied("Only a workspace admin can assign the project admin role.")
            if member and member.member_id == request.user.id and role < 20:
                raise TeamConflict("Do not demote your own project admin membership through Teams.")
            member, created = ProjectMember.objects.update_or_create(
                project=project,
                member_id=user_id,
                defaults={"workspace_id": project.workspace_id, "role": role, "is_active": True},
            )
            ProjectUserProperty.objects.get_or_create(
                project=project, workspace_id=project.workspace_id, user_id=user_id
            )
        return Response(member_data(member), status=201 if request.method == "POST" else 200)

    @transaction.atomic
    def import_legacy(self, request, slug, project_id):
        project = self.project(lock=True)
        if not self.admin(project):
            raise PermissionDenied("A project admin must import the legacy address page.")
        if request.data:
            raise serializers.ValidationError(
                "Import reads the existing project page; no supplied document is accepted."
            )
        receipt = ProjectTeamMigration.objects.filter(project=project).first()
        if receipt:
            return Response({"status": "imported", "page_id": str(receipt.page_id), "changed": False})
        page_ids = list(legacy_pages(project).values_list("id", flat=True)[:2])
        if len(page_ids) != 1:
            raise TeamConflict("Import requires exactly one active hcom team page.")
        page = get_object_or_404(Page.objects.select_for_update(), id=page_ids[0])
        # Discovery may have waited for a concurrent archive/rename. Recheck
        # after acquiring the row lock so old clients can still find the source.
        if list(legacy_pages(project).values_list("id", flat=True)[:2]) != [page.id]:
            raise TeamConflict("The legacy page changed while import was waiting; reread the project before importing.")
        if page.access != Page.PUBLIC_ACCESS and page.owned_by_id != request.user.id:
            raise PermissionDenied("The legacy page is private to another member.")
        document, _ = parse_document(page.description_html, project)
        created = 0
        for address, value in document["contacts"].items():
            actor = ProjectAgentRegistration.objects.filter(project=project, id=value["registration_id"]).first()
            if (
                not actor
                or str(actor.user_id) != value["user_id"]
                or actor.runtime != value["runtime"]
                or actor.agent_key != value["agent_key"]
            ):
                raise TeamConflict(
                    "A legacy contact references an unavailable or different project identity; "
                    "reconcile it before import."
                )
            previous = ProjectTeamContact.objects.filter(project=project, address=address).first()
            if previous and (
                previous.registration_id != actor.id or previous.target != value["target"] or not previous.active
            ):
                raise TeamConflict("A legacy contact conflicts with a canonical address; no contacts were imported.")
            if not previous:
                ProjectTeamContact.objects.create(
                    project=project, address=address, registration=actor, target=value["target"]
                )
                created += 1
        ProjectTeamMigration.objects.create(
            project=project, page_id=page.id, source_revision=page.updated_at.isoformat()
        )
        # Lock the preserved document so the ordinary browser editor cannot become
        # a second writer. No content, ownership or archival state is changed.
        page.is_locked = True
        page.save(update_fields=("is_locked",))
        return Response({"status": "imported", "page_id": str(page.id), "changed": True, "imported": created})
