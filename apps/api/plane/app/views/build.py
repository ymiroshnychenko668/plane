"""Shared build reads and transactional evidence publication."""

import re
from datetime import timezone as datetime_timezone
from uuid import UUID

from django.db import transaction
from django.db.models import Prefetch
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import serializers, status
from rest_framework.exceptions import APIException
from rest_framework.response import Response

from plane.app.permissions import ProjectEntityPermission
from plane.app.serializers.build import BuildPublishSerializer, BuildReadSerializer
from plane.app.views.base import BaseViewSet
from plane.db.models import Build, BuildResult, Module, Project


class BuildConflict(APIException):
    status_code = 409
    default_detail = "This observation conflicts with the recorded build evidence."


class BuildPreconditionFailed(APIException):
    status_code = 412
    default_detail = "Build version changed; read the current build before publishing again."


class BuildPreconditionRequired(APIException):
    status_code = 428
    default_detail = 'If-Match is required; use "0" to create or the current build ETag to update.'


def expected_build_version(request):
    value = request.headers.get("If-Match")
    if value is None:
        raise BuildPreconditionRequired()
    match = re.fullmatch(r'(?:W/)?"(0|[1-9][0-9]{0,18})"|(0|[1-9][0-9]{0,18})', value.strip())
    if not match:
        raise serializers.ValidationError({"If-Match": "Expected one quoted numeric build version."})
    return int(match.group(1) or match.group(2))


def build_response(build, response_status=status.HTTP_200_OK):
    response = Response(BuildReadSerializer(build).data, status=response_status)
    response["ETag"] = f'"{build.version}"'
    response["Cache-Control"] = "no-store"
    return response


class BuildViewSet(BaseViewSet):
    model = Build
    serializer_class = BuildReadSerializer
    permission_classes = [ProjectEntityPermission]
    use_read_replica = False

    def get_queryset(self):
        queryset = Build.objects.filter(project_id=self.project_id, workspace__slug=self.workspace_slug)
        for key in ("repository", "ref", "workflow", "status", "run_id"):
            value = self.request.query_params.get(key)
            if value:
                if key == "status" and value not in {"unknown", "queued", "running", "completed"}:
                    raise serializers.ValidationError({key: "Unknown build status."})
                queryset = queryset.filter(**{key: value.lower() if key == "repository" else value})
        attempt = self.request.query_params.get("attempt")
        if attempt is not None:
            if not re.fullmatch(r"[1-9][0-9]{0,9}", attempt) or int(attempt) > 2147483647:
                raise serializers.ValidationError({"attempt": "Expected a positive integer."})
            queryset = queryset.filter(attempt=int(attempt))
        module = self.request.query_params.get("module_id")
        if module:
            try:
                module_id = None if module == "null" else UUID(module)
            except ValueError:
                raise serializers.ValidationError({"module_id": "Expected a module UUID or null."})
            matches = BuildResult.objects.filter(module_id=module_id).values("build_id")
            queryset = queryset.filter(id__in=matches)
        return (
            queryset.annotate(run_order=Coalesce("started_at", "observed_at"))
            .order_by("-run_order", "-observed_at", "-created_at", "-id")
            .prefetch_related(Prefetch("results", queryset=BuildResult.objects.select_related("module")))
        )

    def list(self, request, slug, project_id):
        get_object_or_404(Project, id=project_id, workspace__slug=slug)
        if self.get_per_page(request, default_per_page=50, max_per_page=100) < 1:
            raise serializers.ValidationError({"per_page": "Expected a positive page size."})
        return self.paginate(
            request=request,
            queryset=self.get_queryset(),
            on_results=lambda rows: BuildReadSerializer(rows, many=True).data,
            default_per_page=50,
            max_per_page=100,
        )

    def retrieve(self, request, slug, project_id, pk):
        return build_response(get_object_or_404(self.get_queryset(), id=pk))

    def summary(self, request, slug, project_id):
        get_object_or_404(Project, id=project_id, workspace__slug=slug)
        builds = self.get_queryset()
        results = BuildResult.objects.filter(build_id__in=builds.values("id"))

        # PostgreSQL DISTINCT ON finds the latest run for every module without
        # imposing a history-page limit or averaging any measurement.
        def latest_ids(measured=False):
            rows = results.exclude(coverage={}) if measured else results
            return dict(
                rows.annotate(build_order=Coalesce("build__started_at", "build__observed_at"))
                .order_by("module_id", "-build_order", "-build__observed_at", "-build_id")
                .distinct("module_id")
                .values_list("module_id", "build_id")
            )

        latest = latest_ids()
        measured = latest_ids(measured=True)
        ids = set(latest.values()) | set(measured.values())
        by_id = {build.id: build for build in builds.filter(id__in=ids)}
        now = timezone.now()

        def item(module_id):
            return {
                "latest_build": BuildReadSerializer(
                    by_id[latest[module_id]], context={"module_id": module_id, "now": now}
                ).data
                if module_id in latest
                else None,
                "last_measured_build": BuildReadSerializer(
                    by_id[measured[module_id]], context={"module_id": module_id, "now": now}
                ).data
                if module_id in measured
                else None,
            }

        modules = Module.objects.filter(project_id=project_id, workspace__slug=slug).order_by(
            "sort_order", "name", "id"
        )
        return Response(
            {
                "modules": [
                    {"module_id": str(module.id), "name": module.name, **item(module.id)} for module in modules
                ],
                "project": item(None),
            }
        )

    @transaction.atomic
    def publish(self, request, slug, project_id):
        expected = expected_build_version(request)
        serializer = BuildPublishSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        result_data = data.pop("results", [])
        # Serializes natural-key creation and result updates for this project.
        project = get_object_or_404(Project.objects.select_for_update(), id=project_id, workspace__slug=slug)
        build = Build.objects.filter(
            project=project, repository=data["repository"], run_id=data["run_id"], attempt=data["attempt"]
        ).first()
        creating = build is None
        if expected != (0 if creating else build.version):
            raise BuildPreconditionFailed()
        if creating:
            required = {"workflow", "revision", "ref", "pipeline_url", "status"}
            missing = required - set(data)
            if missing:
                raise serializers.ValidationError({key: "Required when creating a build." for key in missing})
            build = Build(project=project, **data)
        else:
            for key in ("workflow", "revision", "ref"):
                if key in data and data[key] != getattr(build, key):
                    raise BuildConflict(f"{key} is immutable for an existing run attempt.")
            if data["observed_at"] < build.observed_at:
                raise BuildConflict("An older observation cannot replace a newer one.")
            ranks = {"unknown": 0, "queued": 1, "running": 2, "completed": 3}
            if "status" in data and ranks[data["status"]] < ranks[build.status]:
                raise BuildConflict("A build status cannot regress.")
            if build.conclusion is not None and "conclusion" in data and data["conclusion"] != build.conclusion:
                raise BuildConflict("A recorded conclusion cannot be changed for the same attempt.")

        changed = creating or any(getattr(build, key) != value for key, value in data.items())
        for key, value in data.items():
            setattr(build, key, value)
        if build.started_at and build.finished_at and build.finished_at < build.started_at:
            raise serializers.ValidationError({"finished_at": "Cannot precede started_at."})
        if build.finished_at and build.status != "completed":
            raise serializers.ValidationError({"finished_at": "Only completed builds have a finished_at timestamp."})
        if build.conclusion is not None and build.status != "completed":
            raise serializers.ValidationError({"conclusion": "Only completed builds have a conclusion."})
        module_ids = {row["module_id"] for row in result_data if row["module_id"] is not None}
        if Module.objects.filter(id__in=module_ids, project=project).count() != len(module_ids):
            raise serializers.ValidationError({"results": "Each module must be an active module in this project."})

        if creating:
            build.created_by = request.user
            build.save(disable_auto_set_user=True)
        existing = {(row.module_id, row.suite): row for row in build.results.all()}
        changed_results = []
        for incoming in result_data:
            values = dict(incoming)
            module_id = values.pop("module_id")
            suite = values.pop("suite")
            row = existing.get((module_id, suite))
            new_row = row is None
            if new_row:
                if not {"test_type", "scope"}.issubset(values):
                    raise serializers.ValidationError(
                        {"results": "New module/suite results require test_type and scope."}
                    )
                row = BuildResult(build=build, project=project, module_id=module_id, suite=suite)
            else:
                for key in ("test_type", "scope", "exclusions"):
                    if key in values and values[key] != getattr(row, key):
                        raise BuildConflict(
                            f"Result {key} is immutable for the same module/suite; use its original measured scope."
                        )
            row_changed = new_row
            if "coverage" in values:
                coverage = dict(row.coverage or {})
                for metric, value in values.pop("coverage").items():
                    if value["measured_at"] > build.observed_at:
                        raise serializers.ValidationError({"coverage": "measured_at cannot be after observed_at."})
                    previous = coverage.get(metric)
                    if previous and value["measured_at"] < parse_datetime(previous["measured_at"]):
                        raise BuildConflict("Older metric evidence cannot replace a newer measurement.")
                    value["measured_at"] = (
                        value["measured_at"].astimezone(datetime_timezone.utc).isoformat().replace("+00:00", "Z")
                    )
                    coverage[metric] = value
                values["coverage"] = coverage
            if values.get("tests") is not None and "tests" in values:
                values["tests"] = {**(row.tests or {}), **values["tests"]}
            for key, value in values.items():
                row_changed = row_changed or getattr(row, key) != value
                setattr(row, key, value)
            if row_changed:
                if new_row:
                    row.created_by = request.user
                else:
                    row.updated_by = request.user
                changed_results.append(row)
        if changed_results:
            changed = True
            for row in changed_results:
                row.save(disable_auto_set_user=True)
        if changed and not creating:
            build.version += 1
            build.updated_by = request.user
            build.save(disable_auto_set_user=True)
        # Reload the prefetch after publication so the response includes every
        # preserved suite/metric, including rows omitted by this writer.
        build = Build.objects.prefetch_related(
            Prefetch("results", queryset=BuildResult.objects.select_related("module"))
        ).get(id=build.id)
        return build_response(build, status.HTTP_201_CREATED if creating else status.HTTP_200_OK)
