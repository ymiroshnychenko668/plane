# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Small, conditional project agent registry. It stores setup evidence, not liveness."""

import json
import re
from datetime import timedelta, timezone as datetime_timezone

from django.db import transaction
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from rest_framework import serializers, status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from plane.app.permissions import ProjectEntityPermission
from plane.db.models import Project, ProjectAgentRegistration

from .base import BaseAPIView
from .management import APIKeyManagementMixin


class RegistrationPreconditionRequired(APIException):
    status_code = status.HTTP_428_PRECONDITION_REQUIRED
    default_detail = 'Send If-Match with the current revision, or "0" to create an absent slot.'
    default_code = "precondition_required"


class RegistrationRevisionConflict(APIException):
    status_code = status.HTTP_412_PRECONDITION_FAILED
    default_detail = "The registration changed. Read its current revision before writing."
    default_code = "revision_conflict"


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise ValidationError({"non_field_errors": ["Expected an object."]})
        if set(data) - set(self.fields):
            raise ValidationError(
                {"non_field_errors": ["Unknown fields are not accepted; do not include credentials."]}
            )
        return super().to_internal_value(data)


class StrictCharField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class IdentifierField(StrictCharField):
    def __init__(self, **kwargs):
        super().__init__(max_length=128, trim_whitespace=False, **kwargs)

    def run_validators(self, value):
        super().run_validators(value)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]*", value):
            raise ValidationError("Use a bounded identifier without whitespace or query parameters.")


class StrictUUIDField(serializers.UUIDField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class BindingSerializer(StrictSerializer):
    binding_id = StrictUUIDField()
    host_id = IdentifierField(required=False)
    thread_id = IdentifierField(required=False)
    agent_id = IdentifierField(required=False)
    session_id = IdentifierField(required=False)

    def validate(self, data):
        runtime = self.context["runtime"]
        required = "thread_id" if runtime == "codex" else "agent_id"
        disallowed = {"agent_id", "session_id"} if runtime == "codex" else {"thread_id"}
        if required not in data:
            raise ValidationError({required: "This field is required for this runtime."})
        if disallowed.intersection(data):
            raise ValidationError("The binding contains fields for another runtime.")
        data["binding_id"] = str(data["binding_id"])
        return data


class SkillSerializer(StrictSerializer):
    id = IdentifierField()
    sha256 = StrictCharField(min_length=64, max_length=64, trim_whitespace=False)

    def validate_sha256(self, value):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValidationError("Expected a lowercase SHA-256 digest.")
        return value


class SetupSerializer(StrictSerializer):
    bundle_revision = StrictCharField(max_length=64, trim_whitespace=False)
    skills = SkillSerializer(many=True, max_length=128)
    mcp_servers = serializers.ListField(child=IdentifierField(), max_length=64)
    verified_at = StrictCharField(max_length=40, allow_null=True, trim_whitespace=False)

    def validate_bundle_revision(self, value):
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise ValidationError("Expected a full lowercase Git commit digest.")
        return value

    def validate_verified_at(self, value):
        if value is None:
            return None
        try:
            parsed = parse_datetime(value)
        except ValueError:
            parsed = None
        if parsed is None or parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise ValidationError("Expected an ISO 8601 timestamp with explicit UTC timezone.")
        return parsed.isoformat().replace("+00:00", "Z")

    def validate_skills(self, value):
        if len({item["id"] for item in value}) != len(value):
            raise ValidationError("Skill IDs must be unique.")
        return sorted(value, key=lambda item: item["id"])

    def validate_mcp_servers(self, value):
        if len(set(value)) != len(value):
            raise ValidationError("MCP server IDs must be unique.")
        return sorted(value)


class RegistrationWriteSerializer(StrictSerializer):
    display_name = StrictCharField(max_length=255)
    binding = BindingSerializer()
    setup = SetupSerializer()
    active = serializers.BooleanField(default=True)

    def to_internal_value(self, data):
        if isinstance(data, dict) and len(json.dumps(data, ensure_ascii=False).encode("utf-8")) > 65536:
            raise ValidationError({"non_field_errors": ["Registration payload must not exceed 64 KiB."]})
        if isinstance(data, dict) and "active" in data and not isinstance(data["active"], bool):
            raise ValidationError({"active": "Expected a boolean."})
        return super().to_internal_value(data)


class RegistrationReadSerializer(serializers.ModelSerializer):
    project = serializers.UUIDField(source="project_id")
    workspace = serializers.UUIDField(source="project.workspace_id")
    created_at = serializers.DateTimeField(default_timezone=datetime_timezone.utc)
    updated_at = serializers.DateTimeField(default_timezone=datetime_timezone.utc)

    class Meta:
        model = ProjectAgentRegistration
        fields = (
            "id",
            "project",
            "workspace",
            "runtime",
            "agent_key",
            "revision",
            "created_at",
            "updated_at",
            "display_name",
            "binding",
            "setup",
            "active",
        )


def validate_slot(runtime, agent_key):
    if runtime not in {"codex", "hermes"}:
        raise ValidationError({"runtime": "Expected codex or hermes."})
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", agent_key):
        raise ValidationError({"agent_key": "Expected a lowercase role key (maximum 64 characters)."})
    if runtime == "codex" and agent_key != "project":
        raise ValidationError({"agent_key": "Codex uses the single project slot."})


def expected_revision(request):
    value = request.headers.get("If-Match")
    if value is None:
        raise RegistrationPreconditionRequired()
    # GZipMiddleware weakens representation ETags. The stored revision remains
    # an exact CAS token, including when copied from a compressed GET response.
    match = re.fullmatch(r'(?:W/)?"(0|[1-9][0-9]{0,18})"', value.strip())
    if match is None:
        raise ValidationError({"If-Match": 'Expected one quoted numeric revision; use "0" for an absent slot.'})
    return int(match.group(1))


class ProjectAgentRegistrationAPIEndpoint(APIKeyManagementMixin, BaseAPIView):
    permission_classes = [ProjectEntityPermission]
    http_method_names = ["get", "put", "head", "options"]

    @staticmethod
    def registration_response(registration, response_status=status.HTTP_200_OK):
        response = Response(RegistrationReadSerializer(registration).data, status=response_status)
        response["ETag"] = f'"{registration.revision}"'
        response["Cache-Control"] = "no-store"
        return response

    def get(self, request, slug, project_id, runtime=None, agent_key=None):
        project = get_object_or_404(Project, id=project_id, workspace__slug=slug)
        registrations = ProjectAgentRegistration.objects.filter(project=project).select_related("project")
        if runtime is None:
            return Response(
                RegistrationReadSerializer(registrations, many=True).data, headers={"Cache-Control": "no-store"}
            )
        validate_slot(runtime, agent_key)
        registration = get_object_or_404(registrations, runtime=runtime, agent_key=agent_key)
        return self.registration_response(registration)

    def put(self, request, slug, project_id, runtime=None, agent_key=None):
        if runtime is None:
            return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)
        validate_slot(runtime, agent_key)
        expected = expected_revision(request)
        serializer = RegistrationWriteSerializer(data=request.data, context={"runtime": runtime})
        serializer.is_valid(raise_exception=True)
        payload = serializer.validated_data
        with transaction.atomic():
            # The parent row exists even for a new slot. Locking it serializes
            # first creation as well as updates without a check/create race.
            # The UNIQUE constraint remains the final invariant for all writers.
            project = get_object_or_404(
                Project.objects.select_for_update(of=("self",)), id=project_id, workspace__slug=slug
            )
            registration = ProjectAgentRegistration.objects.filter(
                project=project, runtime=runtime, agent_key=agent_key
            ).first()
            actual = registration.revision if registration else 0
            if expected != actual:
                raise RegistrationRevisionConflict()
            if registration is None:
                registration = ProjectAgentRegistration.objects.create(
                    project=project, runtime=runtime, agent_key=agent_key, **payload
                )
                return self.registration_response(registration, status.HTTP_201_CREATED)
            if all(getattr(registration, key) == value for key, value in payload.items()):
                return self.registration_response(registration)
            for key, value in payload.items():
                setattr(registration, key, value)
            registration.revision += 1
            registration.save(update_fields=[*payload, "revision", "updated_at"])
            return self.registration_response(registration)
