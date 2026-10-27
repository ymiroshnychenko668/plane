# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Join a Codex chat to an assignable identity in an existing project."""

import json
import re
from uuid import NAMESPACE_URL, uuid5

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from plane.app.permissions import ProjectAdminPermission, ProjectEntityPermission
from plane.db.models import APIToken, Project, ProjectAgentRegistration, ProjectMember, User, WorkspaceMember

from .agent_registration import (
    IdentifierField,
    RegistrationReadSerializer,
    RegistrationRevisionConflict,
    SetupSerializer,
    StrictCharField,
    StrictSerializer,
    expected_revision,
)
from .base import BaseAPIView
from .management import APIKeyManagementMixin
from .project_provisioning import ProvisioningConflict

CHAT_KIND = "agent-platform-codex-chat"
IDENTITY_KIND = "agent-platform-codex-identity"


class ChatAgentInput(StrictSerializer):
    display_name = StrictCharField(max_length=255)
    role = StrictCharField(max_length=4096)
    identity_key = StrictCharField(max_length=64, default="project", trim_whitespace=False)
    host_id = IdentifierField(required=False)
    setup = SetupSerializer(required=False)

    def validate_identity_key(self, value):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) or value.startswith("chat-"):
            raise ValidationError("Select a stable identity role key; chat-* is reserved for chat addresses.")
        return value

    def to_internal_value(self, data):
        if isinstance(data, dict) and len(json.dumps(data, ensure_ascii=False).encode()) > 65536:
            raise ValidationError("Chat registration payload must not exceed 64 KiB.")
        return super().to_internal_value(data)


def chat_agent_key(thread_id):
    return f"chat-{thread_id.hex}"


def ensure_identity_membership(model, user, **scope):
    member, created = model.objects.get_or_create(member=user, **scope, defaults={"role": 15, "is_active": True})
    changed = not created and (member.role < 15 or not member.is_active)
    if changed:
        member.role, member.is_active = max(15, member.role), True
        member.save(update_fields=["role", "is_active", "updated_at"])
    return member, created or changed


def check_identity(identity):
    user, token = identity.user, identity.api_token
    stable_id = uuid5(NAMESPACE_URL, f"plane:{identity.project_id}:codex:{identity.agent_key}").hex
    username = f"agent_codex_{stable_id}"
    if (
        not identity.active
        or not user
        or user.username != username
        or user.email != f"{username}@agents.plane.invalid"
        or not user.is_active
        or not user.is_managed
        or user.is_bot
        or user.has_usable_password()
    ):
        raise ProvisioningConflict("The selected account no longer matches its managed identity.")
    if token and (
        not token.is_active
        or token.deleted_at
        or token.user_id != user.id
        or token.workspace_id != identity.project.workspace_id
        or (token.expired_at is not None and token.expired_at <= timezone.now())
    ):
        raise ProvisioningConflict("The identity credential is inactive or belongs to another identity.")


def ensure_identity(project, identity_key, *, setup=None):
    identity, created = ProjectAgentRegistration.objects.get_or_create(
        project=project,
        runtime="codex",
        agent_key=identity_key,
        defaults={
            "display_name": f"Codex {identity_key}",
            "provisioning": {"kind": IDENTITY_KIND},
            "setup": setup if setup is not None else {},
        },
    )
    if identity.provisioning.get("kind") == CHAT_KIND:
        raise ProvisioningConflict("The selected identity is a chat address slot.")
    changed = created
    if identity.user_id is None:
        stable_id = uuid5(NAMESPACE_URL, f"plane:{project.id}:codex:{identity_key}").hex
        username = f"agent_codex_{stable_id}"
        email = f"{username}@agents.plane.invalid"
        if User.objects.filter(Q(username=username) | Q(email=email)).exists():
            raise ProvisioningConflict("The reserved identity account already exists without its registration.")
        user = User(
            username=username,
            email=email,
            display_name=identity.display_name,
            first_name=identity.display_name,
            is_managed=True,
            is_bot=False,
            is_active=True,
        )
        user.set_unusable_password()
        user.save()
        identity.user = user
        changed = True
    check_identity(identity)
    for scope, model in (({"workspace": project.workspace}, WorkspaceMember), ({"project": project}, ProjectMember)):
        _, membership_changed = ensure_identity_membership(model, identity.user, **scope)
        changed = changed or membership_changed
    if identity.api_token_id is None:
        identity.api_token = APIToken.objects.create(
            user=identity.user,
            workspace=project.workspace,
            user_type=1,
            is_service=True,
            label=f"agent-platform:{project.id}:codex:{identity_key}",
        )
        changed = True
    if changed:
        if not created:
            identity.revision += 1
        identity.save()
    return identity


def check_chat_identity(registration, thread_id, identity):
    if (
        registration.provisioning.get("kind") != CHAT_KIND
        or registration.provisioning.get("thread_id") != str(thread_id)
        or registration.provisioning.get("identity_key") != identity.agent_key
        or registration.binding.get("thread_id") != str(thread_id)
        or registration.user_id != identity.user_id
    ):
        raise ProvisioningConflict("This chat slot belongs to another thread or selected identity.")


class ProjectChatAgentAPIEndpoint(APIKeyManagementMixin, BaseAPIView):
    permission_classes = [ProjectEntityPermission]
    http_method_names = ["get", "put", "head", "options"]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def require_admin(self, request):
        if not ProjectAdminPermission().has_permission(request, self):
            raise PermissionDenied("A project admin must join a chat or recover an identity credential.")

    @staticmethod
    def representation(registration, identity, *, credential=False, response_status=status.HTTP_200_OK):
        project = registration.project
        member = ProjectMember.objects.filter(project=project, member_id=identity.user_id).first()
        workspace_member = WorkspaceMember.objects.filter(
            workspace=project.workspace, member_id=identity.user_id
        ).first()
        data = {
            "schema_version": 1,
            "project_id": str(project.id),
            "agent": RegistrationReadSerializer(registration).data,
            "identity": RegistrationReadSerializer(identity).data,
            "membership": {
                "id": str(member.id) if member else None,
                "role": member.role if member else None,
                "is_active": bool(member and member.is_active),
                "workspace_membership_id": str(workspace_member.id) if workspace_member else None,
            },
        }
        if credential:
            if not identity.api_token_id:
                raise ProvisioningConflict("Repeat registration to restore the missing identity credential.")
            data["credential"] = {"api_key": identity.api_token.token}
        return Response(data, status=response_status, headers={"ETag": f'"{registration.revision}"'})

    def get(self, request, slug, project_id, thread_id):
        include_credential = request.query_params.get("include_credential", "false")
        if include_credential not in {"true", "false"}:
            raise ValidationError({"include_credential": "Expected true or false."})
        if include_credential == "true":
            self.require_admin(request)
        project = get_object_or_404(Project, id=project_id, workspace__slug=slug)
        registration = get_object_or_404(
            ProjectAgentRegistration, project=project, runtime="codex", agent_key=chat_agent_key(thread_id)
        )
        identity = get_object_or_404(
            ProjectAgentRegistration,
            project=project,
            runtime="codex",
            agent_key=registration.provisioning.get("identity_key"),
        )
        check_identity(identity)
        check_chat_identity(registration, thread_id, identity)
        return self.representation(registration, identity, credential=include_credential == "true")

    def put(self, request, slug, project_id, thread_id):
        self.require_admin(request)
        expected = expected_revision(request)
        serializer = ChatAgentInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        key = chat_agent_key(thread_id)
        with transaction.atomic():
            project = get_object_or_404(
                Project.objects.select_for_update(of=("self",)), id=project_id, workspace__slug=slug
            )
            if project.archived_at:
                raise ProvisioningConflict("Restore the existing project before joining a chat.")
            registration = ProjectAgentRegistration.objects.filter(
                project=project, runtime="codex", agent_key=key
            ).first()
            if expected != (registration.revision if registration else 0):
                raise RegistrationRevisionConflict()
            identity = ensure_identity(project, payload["identity_key"], setup=payload.get("setup"))
            created = registration is None
            if created:
                registration = ProjectAgentRegistration(
                    project=project,
                    runtime="codex",
                    agent_key=key,
                    user=identity.user,
                    binding={
                        "binding_id": str(uuid5(NAMESPACE_URL, f"plane-chat:{project.id}:{thread_id}")),
                        "thread_id": str(thread_id),
                    },
                    provisioning={"kind": CHAT_KIND, "thread_id": str(thread_id), "identity_key": identity.agent_key},
                )
            else:
                check_chat_identity(registration, thread_id, identity)
            changed = created or registration.api_token_id != identity.api_token_id
            registration.api_token = identity.api_token
            binding = dict(registration.binding)
            if "host_id" in payload:
                binding["host_id"] = payload["host_id"]
            values = {
                "display_name": payload["display_name"],
                "binding": binding,
                "provisioning": {**registration.provisioning, "role": payload["role"]},
                "active": True,
            }
            if "setup" in payload:
                values["setup"] = payload["setup"]
            for field, value in values.items():
                if getattr(registration, field) != value:
                    setattr(registration, field, value)
                    changed = True
            if changed:
                if not created:
                    registration.revision += 1
                registration.save()
            return self.representation(
                registration,
                identity,
                credential=True,
                response_status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
