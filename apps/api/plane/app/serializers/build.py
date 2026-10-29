"""Typed, evidence-preserving build publication and read representations."""

import math
import re
from datetime import datetime, timezone as datetime_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers

from plane.db.models import Build


class StrictSerializer(serializers.Serializer):
    def to_internal_value(self, data):
        if isinstance(data, dict) and set(data) - set(self.fields):
            raise serializers.ValidationError({key: "Unknown field." for key in set(data) - set(self.fields)})
        return super().to_internal_value(data)


class TextField(serializers.CharField):
    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


class CountField(serializers.IntegerField):
    def __init__(self, **kwargs):
        kwargs.setdefault("min_value", 0)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        if isinstance(data, bool) or not isinstance(data, int):
            raise serializers.ValidationError("Expected an integer count.")
        return super().to_internal_value(data)


class TimestampField(serializers.DateTimeField):
    def __init__(self, **kwargs):
        super().__init__(default_timezone=datetime_timezone.utc, **kwargs)

    def to_internal_value(self, data):
        try:
            parsed = data if isinstance(data, datetime) else parse_datetime(data)
        except (TypeError, ValueError):
            parsed = None
        if parsed is None or timezone.is_naive(parsed):
            raise serializers.ValidationError("Expected an ISO 8601 timestamp with an explicit timezone.")
        return super().to_internal_value(data)


class RevisionField(TextField):
    def __init__(self, **kwargs):
        super().__init__(max_length=64, **kwargs)

    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise serializers.ValidationError("Expected a full lowercase Git commit digest.")
        return value


class CoverageMetricSerializer(StrictSerializer):
    covered = CountField()
    total = CountField()
    revision = RevisionField()
    tool = TextField(max_length=255)
    measured_at = TimestampField()
    source = TextField(max_length=4096)

    def validate(self, attrs):
        if attrs["covered"] > attrs["total"]:
            raise serializers.ValidationError("covered cannot exceed total.")
        return attrs


class CoverageSerializer(StrictSerializer):
    line = CoverageMetricSerializer(required=False)
    branch = CoverageMetricSerializer(required=False)
    function = CoverageMetricSerializer(required=False)
    statement = CoverageMetricSerializer(required=False)


class TestsSerializer(StrictSerializer):
    passed = CountField(required=False)
    failed = CountField(required=False)
    skipped = CountField(required=False)


class BuildResultWriteSerializer(StrictSerializer):
    # Null is an explicit project aggregate; omission is refused to avoid accidental aggregates.
    module_id = serializers.UUIDField(allow_null=True)
    suite = TextField(max_length=255)
    test_type = serializers.ChoiceField(choices=("unit", "integration", "e2e", "other"), required=False)
    scope = TextField(required=False)
    exclusions = serializers.ListField(child=TextField(), required=False)
    tests = TestsSerializer(required=False, allow_null=True)
    coverage = CoverageSerializer(required=False)


class BuildLinkSerializer(StrictSerializer):
    title = TextField(max_length=255)
    url = serializers.URLField(max_length=4096)

    def validate_url(self, value):
        if not value.startswith(("https://", "http://")):
            raise serializers.ValidationError("Expected an HTTP(S) URL.")
        return value


class BuildPublishSerializer(StrictSerializer):
    repository = TextField(max_length=255)
    run_id = TextField(max_length=128)
    attempt = CountField(min_value=1, max_value=2147483647)
    workflow = TextField(max_length=255, required=False)
    revision = RevisionField(required=False)
    ref = TextField(max_length=512, required=False)
    pipeline_url = serializers.URLField(max_length=2048, required=False)
    status = serializers.ChoiceField(choices=("queued", "running", "completed", "unknown"), required=False)
    conclusion = TextField(max_length=64, required=False, allow_null=True)
    started_at = TimestampField(required=False, allow_null=True)
    finished_at = TimestampField(required=False, allow_null=True)
    observed_at = TimestampField()
    freshness_hours = serializers.FloatField(min_value=0, required=False, allow_null=True)
    links = BuildLinkSerializer(many=True, required=False)
    results = BuildResultWriteSerializer(many=True, required=False)

    def validate_repository(self, value):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            raise serializers.ValidationError("Expected owner/repository.")
        return value.lower()

    def validate_pipeline_url(self, value):
        if not value.startswith(("https://", "http://")):
            raise serializers.ValidationError("Expected an HTTP(S) URL.")
        return value

    def validate_freshness_hours(self, value):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise serializers.ValidationError("Expected a positive finite policy value or null.")
        return value

    def validate_results(self, value):
        identities = [(row["module_id"], row["suite"]) for row in value]
        if len(identities) != len(set(identities)):
            raise serializers.ValidationError("Each module/suite may appear once per publication.")
        return value


def metric_representation(metric, build, now):
    result = dict(metric)
    result["percent"] = round(100 * metric["covered"] / metric["total"], 2) if metric["total"] else None
    states = []
    if metric["revision"] != build.revision:
        states.append("revision_mismatch")
    measured = parse_datetime(metric["measured_at"])
    if build.freshness_hours is not None and (now - measured).total_seconds() > build.freshness_hours * 3600:
        states.append("stale")
    result["evidence_states"] = states or ["measured"]
    return result


class BuildReadSerializer(serializers.ModelSerializer):
    results = serializers.SerializerMethodField()
    started_at = TimestampField(allow_null=True)
    finished_at = TimestampField(allow_null=True)
    observed_at = TimestampField()
    created_at = TimestampField()
    updated_at = TimestampField()
    duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Build
        fields = (
            "id",
            "project_id",
            "repository",
            "workflow",
            "run_id",
            "attempt",
            "revision",
            "ref",
            "pipeline_url",
            "status",
            "conclusion",
            "started_at",
            "finished_at",
            "observed_at",
            "freshness_hours",
            "links",
            "version",
            "duration_seconds",
            "created_at",
            "updated_at",
            "created_by_id",
            "updated_by_id",
            "results",
        )

    def get_duration_seconds(self, build):
        if build.started_at is None or build.finished_at is None:
            return None
        return (build.finished_at - build.started_at).total_seconds()

    def get_results(self, build):
        now = self.context.get("now") or timezone.now()
        rows = []
        for row in build.results.all():
            if "module_id" in self.context and row.module_id != self.context["module_id"]:
                continue
            coverage = {key: metric_representation(value, build, now) for key, value in row.coverage.items()}
            states = sorted({state for metric in coverage.values() for state in metric["evidence_states"]})
            rows.append(
                {
                    "module_id": str(row.module_id) if row.module_id else None,
                    "module_name": row.module.name if row.module_id else None,
                    "suite": row.suite,
                    "test_type": row.test_type,
                    "scope": row.scope,
                    "exclusions": row.exclusions,
                    "tests": row.tests,
                    "coverage": coverage,
                    "evidence_states": states or ["not_measured"],
                }
            )
        return rows
