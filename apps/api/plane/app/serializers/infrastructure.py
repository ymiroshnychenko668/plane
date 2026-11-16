"""Strict, bounded inventory metadata. Logical references never resolve secrets."""

import re
from datetime import timezone as datetime_timezone
from urllib.parse import unquote, urlsplit

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from plane.db.models.infrastructure import LIFECYCLES, RELATION_KINDS, RESOURCE_KINDS


SLUG = r"[a-z0-9][a-z0-9._:-]*"
KEY = r"[a-z0-9][a-z0-9._:/-]*"
SECRET = re.compile(
    r"(?:-----BEGIN [A-Z ]*PRIVATE KEY-----|\bbearer\s+\S+|"
    r"\b(?:password|passwd|token|api[_-]?key|api[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"client[_-]?secret|secret|authorization)\s*[:=]\s*\S+|"
    r"\b[a-z][a-z0-9+.-]*://[^\s/?#]*@)",
    re.IGNORECASE,
)


def safe_text(value):
    if not isinstance(value, str):
        raise serializers.ValidationError("Expected a string.")
    decoded = unquote(unquote(value))
    if any(ord(char) < 32 or ord(char) == 127 for char in value) or SECRET.search(decoded):
        raise serializers.ValidationError("Control characters and credential values are not allowed.")
    return value


class SafeText(serializers.CharField):
    def to_internal_value(self, data):
        safe_text(data)
        return super().to_internal_value(data)


class Slug(SafeText):
    def __init__(self, *args, pattern=SLUG, **kwargs):
        self.pattern = pattern
        super().__init__(*args, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not re.fullmatch(self.pattern, value):
            raise serializers.ValidationError("Expected a valid lowercase identifier.")
        return value


class Choice(serializers.ChoiceField):
    default_error_messages = {"invalid_choice": "Unknown choice."}

    def to_internal_value(self, data):
        safe_text(data)
        return super().to_internal_value(data)


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if not isinstance(data, dict):
            raise serializers.ValidationError({"non_field_errors": ["Expected an object."]})
        if set(data) - set(self.fields):
            raise serializers.ValidationError({"non_field_errors": ["Unknown properties are not allowed."]})
        return super().to_internal_value(data)


class ObservedDateTime(serializers.DateTimeField):
    def to_internal_value(self, data):
        try:
            parsed = parse_datetime(data) if isinstance(data, str) else None
        except (ValueError, TypeError):
            parsed = None
        if not parsed or timezone.is_naive(parsed):
            raise serializers.ValidationError("Expected a timezone-aware ISO 8601 timestamp.")
        if parsed > timezone.now():
            raise serializers.ValidationError("Future observations are not allowed.")
        return parsed.astimezone(datetime_timezone.utc)


class UUIDInput(serializers.UUIDField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            raise serializers.ValidationError("Expected a UUID string.")
        return super().to_internal_value(data)


class OwnerInput(StrictSerializer):
    name = SafeText(max_length=255)
    reference = SafeText(max_length=512)
    recovery_contact = SafeText(max_length=512)


class SourceInput(StrictSerializer):
    kind = Choice(choices=("git", "manual", "import", "api"))
    reference = SafeText(max_length=1024)
    revision = SafeText(max_length=255)


class EvidenceInput(StrictSerializer):
    kind = Choice(choices=("review", "observation", "document"))
    reference = SafeText(max_length=1024)
    observed_at = ObservedDateTime()

    def validate(self, data):
        data["observed_at"] = data["observed_at"].isoformat().replace("+00:00", "Z")
        return data


class CredentialReferenceInput(StrictSerializer):
    provider = Choice(choices=("openbao",))
    path = SafeText(max_length=512)
    field = Slug(max_length=128)
    purpose = SafeText(max_length=512)

    def validate_path(self, value):
        if any(not re.fullmatch(r"[A-Za-z0-9._-]+", segment) or segment in {".", ".."} for segment in value.split("/")):
            raise serializers.ValidationError("Expected a logical OpenBao path, not a URL or traversal.")
        return value


class UniqueSlugList(serializers.ListField):
    def to_internal_value(self, data):
        values = super().to_internal_value(data)
        if len(set(values)) != len(values):
            raise serializers.ValidationError("Duplicate identifiers are not allowed.")
        return values


class AccessMethodInput(StrictSerializer):
    id = Slug(max_length=64)
    kind = Choice(choices=("ssh", "http", "https", "postgresql", "mysql", "s3", "smb", "nfs", "console", "other"))
    endpoint = SafeText(max_length=1024)
    capability = Slug(max_length=128)
    operations = UniqueSlugList(child=Slug(max_length=128), min_length=1, max_length=30)
    scope = SafeText(max_length=1024)
    identity = SafeText(max_length=255)
    credential_refs = CredentialReferenceInput(many=True, default=list)
    notes = SafeText(max_length=1000, allow_blank=True, default="")

    def validate_credential_refs(self, value):
        if len(value) > 10:
            raise serializers.ValidationError("At most ten credential references are allowed.")
        return value

    def validate(self, data):
        try:
            endpoint = urlsplit(data["endpoint"])
            expected = "https" if data["kind"] in {"console", "other"} else data["kind"]
            if (
                endpoint.scheme != expected
                or not endpoint.hostname
                or endpoint.username is not None
                or endpoint.password is not None
                or endpoint.query
                or endpoint.fragment
                or any(char.isspace() for char in data["endpoint"])
                or "?" in data["endpoint"]
                or "#" in data["endpoint"]
                or "\\" in data["endpoint"]
            ):
                raise ValueError()
            endpoint.port
        except ValueError:
            raise serializers.ValidationError(
                {"endpoint": "Expected an absolute endpoint without credentials, query or fragment."}
            )
        return data


class EvidenceFields(StrictSerializer):
    source = SourceInput()
    evidence = EvidenceInput(many=True, default=list)
    last_verified = ObservedDateTime(allow_null=True, default=None)

    def validate_evidence(self, value):
        if len(value) > 50:
            raise serializers.ValidationError("At most fifty evidence entries are allowed.")
        return value

    def validate(self, data):
        if data.get("last_verified") is not None and not data.get("evidence"):
            raise serializers.ValidationError({"last_verified": "Verification requires evidence."})
        return data


class ResourceInput(EvidenceFields):
    key = Slug(pattern=KEY, max_length=160)
    name = SafeText(max_length=255)
    kind = Choice(choices=RESOURCE_KINDS)
    lifecycle = Choice(choices=LIFECYCLES, default="planned")
    description = SafeText(max_length=4000, default="", allow_blank=True)
    tags = UniqueSlugList(child=Slug(max_length=64), max_length=20, default=list)
    owner = OwnerInput()
    access_methods = AccessMethodInput(many=True, default=list)

    def validate_access_methods(self, value):
        if len(value) > 20 or len({row["id"] for row in value}) != len(value):
            raise serializers.ValidationError("Use at most twenty methods with unique identifiers.")
        return value


class RelationInput(EvidenceFields):
    source_id = UUIDInput()
    target_id = UUIDInput()
    kind = Choice(choices=RELATION_KINDS)


class ProjectLinkInput(StrictSerializer):
    project_id = UUIDInput()
