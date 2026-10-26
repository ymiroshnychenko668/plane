# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Ensure a repository's Plane project and visible agent account in one transaction."""

import hashlib
import json
import re
from copy import deepcopy
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from plane.api.serializers import ProjectCreateSerializer
from plane.app.permissions import WorkspaceOwnerPermission
from plane.app.serializers import PageBinaryUpdateSerializer, PageSerializer
from plane.db.models import (
    APIToken,
    DEFAULT_STATES,
    Module,
    Page,
    Project,
    ProjectAgentRegistration,
    ProjectMember,
    State,
    User,
    Workspace,
    WorkspaceMember,
)
from plane.utils.host import base_host

from .agent_registration import SetupSerializer, StrictCharField, StrictSerializer, validate_slot
from .base import BaseAPIView
from .management import APIKeyManagementMixin, call_live

SOURCE = "agent-platform"
KEY_PATTERN = r"[a-z0-9][a-z0-9._-]{0,127}"


class ProvisioningConflict(APIException):
    status_code = status.HTTP_409_CONFLICT
    default_detail = "The existing project or agent does not match this provisioning request."
    default_code = "provisioning_conflict"


class ProvisioningCodecError(APIException):
    status_code = status.HTTP_502_BAD_GATEWAY
    default_detail = "The document codec failed; no provisioning changes were applied."
    default_code = "provisioning_codec_failed"


class KeyField(StrictCharField):
    def __init__(self, **kwargs):
        super().__init__(max_length=128, trim_whitespace=False, **kwargs)

    def run_validators(self, value):
        super().run_validators(value)
        if re.fullmatch(KEY_PATTERN, value) is None:
            raise ValidationError("Expected a stable lowercase key without spaces (maximum 128 characters).")


class ProjectInput(StrictSerializer):
    name = StrictCharField(max_length=255, trim_whitespace=False)
    identifier = StrictCharField(max_length=12, trim_whitespace=False)
    description = StrictCharField(max_length=100000, allow_blank=True, default="", trim_whitespace=False)
    repository_url = StrictCharField(max_length=2048, trim_whitespace=False)
    existing_project_id = serializers.UUIDField(required=False)

    def validate_identifier(self, value):
        if re.fullmatch(r"[A-Z][A-Z0-9_]{0,11}", value) is None:
            raise ValidationError("Expected 1–12 uppercase letters, digits or underscores, starting with a letter.")
        return value

    def validate_repository_url(self, value):
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme == "https"
                and parsed.hostname
                and parsed.netloc == parsed.hostname
                and parsed.path not in {"", "/"}
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and not value.endswith(("/", ".git"))
                and not any(char.isspace() for char in value)
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValidationError(
                "Use a canonical HTTPS repository URL without credentials, query, .git or trailing slash."
            )
        return value


class AgentInput(StrictSerializer):
    runtime = serializers.ChoiceField(choices=("codex", "hermes"))
    key = StrictCharField(max_length=64, trim_whitespace=False)
    display_name = StrictCharField(max_length=255, trim_whitespace=False)

    def validate(self, data):
        validate_slot(data["runtime"], data["key"])
        return data


class ModuleInput(StrictSerializer):
    key = KeyField()
    name = StrictCharField(max_length=255, trim_whitespace=False)
    description = StrictCharField(max_length=100000, allow_blank=True, default="", trim_whitespace=False)


class PageInput(StrictSerializer):
    key = KeyField()
    name = StrictCharField(max_length=255, trim_whitespace=False)
    description_html = StrictCharField(max_length=500000, allow_blank=True, trim_whitespace=False)

    def validate_description_html(self, value):
        # Validate now, but retain the submitted text for the cross-client digest.
        serializer = PageBinaryUpdateSerializer(data={"description_html": value})
        serializer.is_valid(raise_exception=True)
        return value


class ProvisioningInput(StrictSerializer):
    schema_version = serializers.ChoiceField(choices=(1,))
    project = ProjectInput()
    agent = AgentInput()
    modules = ModuleInput(many=True, max_length=100, default=list)
    pages = PageInput(many=True, max_length=100, default=list)
    setup = SetupSerializer(required=False)

    def to_internal_value(self, data):
        if isinstance(data, dict) and len(json.dumps(data, ensure_ascii=False).encode()) > 2_000_000:
            raise ValidationError("Provisioning payload must not exceed 2 MB.")
        return super().to_internal_value(data)

    def validate(self, data):
        for field in ("modules", "pages"):
            values = data[field]
            if len({item["key"] for item in values}) != len(values):
                raise ValidationError({field: "Keys must be unique."})
            if len({item["name"] for item in values}) != len(values):
                raise ValidationError({field: "Names must be unique within the manifest."})
            data[field] = sorted(values, key=lambda item: item["key"])
        return data


def canonical_manifest(payload):
    result = deepcopy(payload)
    result.pop("setup", None)
    result["project"].pop("existing_project_id", None)
    return result


def manifest_digest(payload):
    serialized = json.dumps(canonical_manifest(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def one_or_none(queryset, description):
    values = list(queryset[:2])
    if len(values) > 1:
        raise ProvisioningConflict(f"Multiple {description} match the managed identity.")
    return values[0] if values else None


def project_for_key(workspace, project_key, project_input=None, *, lock=False):
    projects = Project.all_objects
    if lock:
        projects = projects.select_for_update(of=("self",))
    project = one_or_none(
        projects.filter(workspace=workspace, external_source=SOURCE, external_id=project_key), "projects"
    )
    if project and (project.deleted_at or project.archived_at):
        raise ProvisioningConflict("The managed project is deleted or archived; restore it explicitly.")
    existing_id = (project_input or {}).get("existing_project_id")
    if existing_id:
        existing = get_object_or_404(projects, workspace=workspace, id=existing_id, deleted_at__isnull=True)
        if project and project.id != existing.id:
            raise ProvisioningConflict("existing_project_id does not match this repository's project.")
        if existing.archived_at:
            raise ProvisioningConflict("Restore the existing project before adopting it.")
        if (existing.external_source or existing.external_id) and (
            existing.external_source != SOURCE or existing.external_id != project_key
        ):
            raise ProvisioningConflict("The existing project belongs to another external identity.")
        project = existing
    if project and project_input:
        for registration in project.agent_registrations.all():
            metadata = registration.provisioning
            if metadata and (
                metadata.get("project_key") != project_key
                or metadata.get("repository_url") != project_input["repository_url"]
            ):
                raise ProvisioningConflict("The project is already associated with another repository.")
    return project


def managed_page(project, key):
    return one_or_none(
        Page.objects.filter(
            project_pages__project=project,
            project_pages__deleted_at__isnull=True,
            external_source=SOURCE,
            external_id=key,
        ).distinct(),
        "pages",
    )


def prepare_page_content(project, pages):
    converted = {}
    for page in pages:
        if project and managed_page(project, page["key"]):
            continue
        html = PageBinaryUpdateSerializer(data={"description_html": page["description_html"]})
        html.is_valid(raise_exception=True)
        response = call_live(
            "convert-document/",
            {"description_html": html.validated_data["description_html"] or "<p></p>", "variant": "document"},
            mutation=False,
        )
        if (
            response.status_code != 200
            or not isinstance(response.data, dict)
            or not {"description_html", "description_json", "description_binary"}.issubset(response.data)
        ):
            raise ProvisioningCodecError()
        content = PageBinaryUpdateSerializer(data=response.data)
        content.is_valid(raise_exception=True)
        converted[page["key"]] = content.validated_data
    return converted


def ensure_membership(model, user, **scope):
    membership, _ = model.objects.get_or_create(member=user, **scope, defaults={"role": 20, "is_active": True})
    if membership.role != 20 or not membership.is_active:
        membership.role, membership.is_active = 20, True
        membership.save(update_fields=["role", "is_active", "updated_at"])
    return membership


def ensure_agent(project, agent, payload, project_key):
    registration, created = ProjectAgentRegistration.objects.get_or_create(
        project=project,
        runtime=agent["runtime"],
        agent_key=agent["key"],
        defaults={"display_name": agent["display_name"]},
    )
    if registration.user_id is None:
        stable_id = uuid5(NAMESPACE_URL, f"plane:{project.id}:{agent['runtime']}:{agent['key']}").hex
        username = f"agent_{agent['runtime']}_{stable_id}"
        email = f"{username}@agents.plane.invalid"
        if User.objects.filter(Q(username=username) | Q(email=email)).exists():
            raise ProvisioningConflict("The reserved agent account identity already exists without this registration.")
        user = User(
            username=username,
            email=email,
            display_name=agent["display_name"],
            first_name=agent["display_name"],
            is_managed=True,
            is_bot=False,
            is_active=True,
        )
        user.set_unusable_password()
        user.save()
        registration.user = user
    elif not registration.user.is_active or not registration.user.is_managed or registration.user.is_bot:
        raise ProvisioningConflict("The registered agent account is inactive or no longer a visible managed account.")
    user = registration.user
    ensure_membership(WorkspaceMember, user, workspace=project.workspace)
    ensure_membership(ProjectMember, user, project=project)
    if registration.api_token_id is None:
        registration.api_token = APIToken.objects.create(
            user=user,
            workspace=project.workspace,
            user_type=1,
            is_service=True,
            label=f"agent-platform:{project.id}:{agent['runtime']}:{agent['key']}",
        )
    token = registration.api_token
    if (
        not token.is_active
        or token.deleted_at
        or token.user_id != user.id
        or token.workspace_id != project.workspace_id
        or (token.expired_at is not None and token.expired_at <= timezone.now())
    ):
        raise ProvisioningConflict("The agent API credential is inactive or belongs to another identity.")
    provisioning = {
        "project_key": project_key,
        "repository_url": payload["project"]["repository_url"],
        "manifest": canonical_manifest(payload),
        "manifest_digest": manifest_digest(payload),
    }
    changed = (
        registration.provisioning != provisioning
        or not registration.active
        or registration.display_name != user.display_name
    )
    registration.provisioning = provisioning
    registration.active = True
    registration.display_name = user.display_name
    if "setup" in payload:
        changed = changed or registration.setup != payload["setup"]
        registration.setup = payload["setup"]
    if changed and not created:
        registration.revision += 1
    registration.save()
    return registration


def ensure_assets(project, agent_user, payload, converted):
    for module in payload["modules"]:
        existing = one_or_none(
            Module.objects.filter(project=project, external_source=SOURCE, external_id=module["key"]), "modules"
        )
        if existing:
            continue
        if Module.objects.filter(project=project, name=module["name"]).exists():
            raise ProvisioningConflict(f"Module name {module['name']!r} already exists under another identity.")
        Module.objects.create(
            project=project,
            name=module["name"],
            description=module["description"],
            external_source=SOURCE,
            external_id=module["key"],
            lead=agent_user,
        )
    for page in payload["pages"]:
        if managed_page(project, page["key"]):
            continue
        if Page.objects.filter(
            project_pages__project=project, project_pages__deleted_at__isnull=True, name=page["name"]
        ).exists():
            raise ProvisioningConflict(f"Page name {page['name']!r} already exists under another identity.")
        if page["key"] not in converted:
            raise ProvisioningConflict("A page changed during preparation; read the project and try again.")
        serializer = PageSerializer(
            data={"name": page["name"], "access": Page.PUBLIC_ACCESS},
            context={"project_id": project.id, "owned_by_id": agent_user.id, **converted[page["key"]]},
        )
        serializer.is_valid(raise_exception=True)
        # These fields exist on Page; do not broaden the unrelated app serializer.
        serializer.save(external_source=SOURCE, external_id=page["key"])


class ProjectProvisioningAPIEndpoint(APIKeyManagementMixin, BaseAPIView):
    permission_classes = [WorkspaceOwnerPermission]
    http_method_names = ["get", "put", "head", "options"]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def validate_key(project_key):
        if re.fullmatch(KEY_PATTERN, project_key) is None:
            raise ValidationError({"project_key": "Expected a stable lowercase repository key."})

    def representation(self, request, registration):
        project = registration.project
        metadata = registration.provisioning
        if not registration.user_id or not registration.api_token_id or not metadata:
            raise ProvisioningConflict("This legacy registration has not been provisioned.")
        token = registration.api_token
        if (
            not registration.active
            or not registration.user.is_active
            or not token.is_active
            or token.deleted_at
            or token.user_id != registration.user_id
            or token.workspace_id != project.workspace_id
            or (token.expired_at is not None and token.expired_at <= timezone.now())
        ):
            raise ProvisioningConflict("The managed agent or credential is inactive.")
        modules, pages = [], []
        for item in metadata["manifest"]["modules"]:
            resource = one_or_none(
                Module.objects.filter(project=project, external_source=SOURCE, external_id=item["key"]), "modules"
            )
            if resource is None:
                raise ProvisioningConflict("A managed module is missing; repeat provisioning to restore it.")
            modules.append({"key": item["key"], "id": str(resource.id), "name": resource.name})
        for item in metadata["manifest"]["pages"]:
            resource = managed_page(project, item["key"])
            if resource is None:
                raise ProvisioningConflict("A managed page is missing; repeat provisioning to restore it.")
            pages.append({"key": item["key"], "id": str(resource.id), "name": resource.name})
        return {
            "schema_version": 1,
            "project_key": metadata["project_key"],
            "project": {
                "id": str(project.id),
                "name": project.name,
                "identifier": project.identifier,
                "repository_url": metadata["repository_url"],
                "url": (
                    f"{base_host(request=request, is_app=True).rstrip('/')}/"
                    f"{project.workspace.slug}/projects/{project.id}/issues/"
                ),
            },
            "agent": {
                "runtime": registration.runtime,
                "key": registration.agent_key,
                "user_id": str(registration.user_id),
                "email": registration.user.email,
                "display_name": registration.user.display_name,
                "registration_id": str(registration.id),
            },
            "modules": modules,
            "pages": pages,
            "credential": {"api_key": token.token},
            "manifest_digest": metadata["manifest_digest"],
        }

    def get(self, request, slug, project_key):
        self.validate_key(project_key)
        runtime, agent_key = (
            request.query_params.get("runtime", "codex"),
            request.query_params.get("agent_key", "project"),
        )
        validate_slot(runtime, agent_key)
        workspace = get_object_or_404(Workspace, slug=slug)
        project = project_for_key(workspace, project_key)
        if project is None:
            return Response({"detail": "Project has not been provisioned."}, status=404)
        registration = get_object_or_404(
            ProjectAgentRegistration, project=project, runtime=runtime, agent_key=agent_key
        )
        return Response(self.representation(request, registration))

    def put(self, request, slug, project_key):
        self.validate_key(project_key)
        serializer = ProvisioningInput(data=request.data)
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        workspace = get_object_or_404(Workspace, slug=slug)
        project = project_for_key(workspace, project_key, payload["project"])
        converted = prepare_page_content(project, payload["pages"])
        with transaction.atomic():
            # The workspace exists before the project. This lock also serializes
            # first creation, adoption and different runtime roles of one project.
            workspace = Workspace.objects.select_for_update(of=("self",)).get(id=workspace.id)
            project = project_for_key(workspace, project_key, payload["project"], lock=True)
            created = project is None
            if created:
                fields = payload["project"]
                if (
                    Project.objects.filter(workspace=workspace)
                    .filter(Q(name=fields["name"]) | Q(identifier=fields["identifier"]))
                    .exists()
                ):
                    raise ProvisioningConflict(
                        "A project name or identifier already exists; adopt it explicitly by ID."
                    )
                creator = ProjectCreateSerializer(
                    data={
                        "name": fields["name"],
                        "identifier": fields["identifier"],
                        "description": fields["description"],
                        "module_view": True,
                        "page_view": True,
                        "external_source": SOURCE,
                        "external_id": project_key,
                    },
                    context={"workspace_id": workspace.id},
                )
                creator.is_valid(raise_exception=True)
                project = creator.save()
                State.objects.bulk_create(
                    [
                        State(project=project, workspace=workspace, created_by=request.user, **state)
                        for state in DEFAULT_STATES
                    ]
                )
            elif project.external_source != SOURCE or project.external_id != project_key:
                project.external_source, project.external_id = SOURCE, project_key
                project.save(update_fields=["external_source", "external_id", "updated_at"])
            if not project.module_view or not project.page_view:
                project.module_view = project.page_view = True
                project.save(update_fields=["module_view", "page_view", "updated_at"])
            ensure_membership(ProjectMember, request.user, project=project)
            registration = ensure_agent(project, payload["agent"], payload, project_key)
            ensure_assets(project, registration.user, payload, converted)
            response = self.representation(request, registration)
        return Response(response, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)
