"""Project Views: one authorization, definition and evaluator contract for both APIs."""

import hashlib
import json
import re
from datetime import timedelta
from uuid import UUID

from django.core import signing
from django.db import transaction
from django.db.models import BooleanField, DateTimeField, Exists, OuterRef, Prefetch, Q, Value
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.exceptions import APIException

from plane.db.models import (
    Issue,
    IssueAssignee,
    IssueLabel,
    IssueView,
    IssueViewCreateReceipt,
    ModuleIssue,
    ProjectMember,
    UserFavorite,
    UserRecentVisit,
    WorkspaceMember,
)
from plane.db.models.view import get_default_display_filters, get_default_display_properties
from plane.utils.view_filters import POLICY, SOURCE_SCHEMA, apply_ordinary_filters, validate_definition

ORDERINGS = {"id", "-id", "created_at", "-created_at", "name", "-name", "priority", "-priority"}
DEFINITION_FIELDS = {
    "name",
    "description",
    "rich_filters",
    "filter_meta",
    "display_filters",
    "display_properties",
    "access",
}


class ViewError(APIException):
    def __init__(self, status_code, code, detail):
        self.status_code = status_code
        super().__init__({"error": {"code": code, "detail": detail}}, code=code)


def fail(status, code, detail):
    raise ViewError(status, code, detail)


def timestamp(value):
    return value.isoformat().replace("+00:00", "Z") if value else None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def managed(view):
    return bool(view.filter_meta)


def check_revision(request, current):
    header = request.headers.get("If-Match")
    if header is None:
        fail(428, "precondition_required", "If-Match is required for this View.")
    match = re.fullmatch(r'(?:W/)?"([1-9][0-9]{0,18})"', header.strip())
    if not match:
        fail(400, "validation_error", "Expected one quoted positive numeric revision.")
    if int(match.group(1)) != current:
        fail(412, "revision_conflict", "The View changed. Read its current definition before retrying.")


def display_filters(value):
    defaults = get_default_display_filters()
    if not isinstance(value, dict) or set(value) - (set(defaults) | {"sub_group_by"}):
        fail(400, "validation_error", "Unsupported display_filters.")
    result = {**defaults, **value}
    if result["layout"] != "list" or not isinstance(result["order_by"], str) or result["order_by"] not in ORDERINGS:
        fail(400, "unsupported_layout", "Version 1 supports list layout with a declared ordering.")
    if any(result.get(key) is not None for key in ("group_by", "sub_group_by", "type")):
        fail(400, "unsupported_layout", "Grouped and typed layouts are not supported.")
    if result["calendar_date_range"] != "" or any(
        type(result[key]) is not bool for key in ("sub_issue", "show_empty_groups")
    ):
        fail(400, "validation_error", "Invalid display options.")
    return result


def clean_definition(payload, current=None, create=False):
    if not isinstance(payload, dict) or set(payload) - (DEFINITION_FIELDS | ({"request_id"} if create else set())):
        fail(400, "validation_error", "Unknown View fields.")
    result = {key: getattr(current, key) for key in DEFINITION_FIELDS} if current else {}
    result.update({key: value for key, value in payload.items() if key != "request_id"})
    name = result.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 255:
        fail(400, "validation_error", "name must contain 1 to 255 characters.")
    result["name"] = name.strip()
    description = result.setdefault("description", "")
    if not isinstance(description, str) or len(description) > 10000:
        fail(400, "validation_error", "description must be a string of at most 10000 characters.")
    if type(result.setdefault("access", 1)) is not int or result["access"] not in {0, 1}:
        fail(400, "validation_error", "access must be 0 (private) or 1 (public).")
    result["display_filters"] = display_filters(result.get("display_filters", {}))
    properties = result.setdefault("display_properties", get_default_display_properties())
    if (
        not isinstance(properties, dict)
        or set(properties) - set(get_default_display_properties())
        or any(type(v) is not bool for v in properties.values())
    ):
        fail(400, "validation_error", "display_properties must contain supported boolean properties.")
    validate_definition(result.get("rich_filters"), result.get("filter_meta"))
    return result


class ProjectViews:
    def __init__(self, request, slug, project_id):
        self.request = request
        self.actor = request.user
        workspace_ids = WorkspaceMember.objects.filter(
            workspace__slug=slug,
            workspace__deleted_at__isnull=True,
            member=self.actor,
            is_active=True,
        ).values("workspace_id")
        self.member = (
            ProjectMember.objects.filter(
                project_id=project_id,
                member=self.actor,
                is_active=True,
                workspace_id__in=workspace_ids,
                project__workspace_id__in=workspace_ids,
                project__deleted_at__isnull=True,
                project__archived_at__isnull=True,
                role__in=(5, 15, 20),
            )
            .select_related("project", "workspace")
            .first()
        )
        if self.member is None:
            fail(404, "not_found", "Project unavailable.")
        self.project = self.member.project
        self.slug = slug

    def queryset(self):
        result = IssueView.objects.filter(project=self.project, workspace=self.member.workspace)
        result = result.filter(Q(owned_by=self.actor) | Q(access=1))
        if self.member.role == 5 and not self.project.guest_view_all_features:
            result = result.filter(owned_by=self.actor)
        return result.annotate(
            is_favorite=Exists(
                UserFavorite.objects.filter(
                    project=self.project,
                    user=self.actor,
                    entity_type="view",
                    entity_identifier=OuterRef("pk"),
                )
            )
        )

    def get(self, view_id, lock=False):
        queryset = self.queryset()
        if lock:
            queryset = queryset.select_for_update()
        view = queryset.filter(pk=view_id).first()
        if view is None:
            fail(404, "not_found", "View unavailable.")
        return view

    def check_semantics(self, definition):
        days = validate_definition(definition["rich_filters"], definition["filter_meta"])
        if days is not None and self.member.role < 15:
            fail(403, "activity_filter_unavailable", "This filter requires full project source visibility.")
        return days

    def serialize(self, view):
        from plane.app.serializers.view import IssueViewSerializer

        result = dict(IssueViewSerializer(view).data)
        result.update(
            can_edit=view.owned_by_id == self.actor.id and not view.is_locked,
            can_delete=view.owned_by_id == self.actor.id or self.member.role == 20,
            url=f"/{self.slug}/projects/{self.project.id}/views/{view.id}/",
        )
        return result

    def create(self, payload):
        if self.member.role < 15:
            fail(403, "forbidden", "Creating Views requires project member permission.")
        definition = clean_definition(payload, create=True)
        self.check_semantics(definition)
        request_id = payload.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", request_id):
            fail(400, "validation_error", "request_id must be an opaque 8 to 128 character identifier.")
        payload_hash = digest(definition)
        with transaction.atomic():
            # One actor/project create lock also serializes the absent-receipt case.
            ProjectMember.objects.select_for_update().get(pk=self.member.pk)
            receipt = IssueViewCreateReceipt.objects.filter(
                project=self.project, actor=self.actor, request_id=request_id
            ).first()
            if receipt:
                if receipt.payload_hash != payload_hash:
                    fail(409, "request_id_conflict", "request_id was already used for another definition.")
                view = self.get(receipt.view_id)
                return view, False
            view = IssueView(
                **definition,
                workspace=self.member.workspace,
                project=self.project,
                owned_by=self.actor,
                created_by_id=self.actor.id,
            )
            view.save(created_by_id=self.actor.id)
            IssueViewCreateReceipt.objects.create(
                project=self.project,
                actor=self.actor,
                request_id=request_id,
                payload_hash=payload_hash,
                view_id=view.id,
            )
            return view, True

    def update(self, view_id, payload):
        with transaction.atomic():
            view = self.get(view_id, lock=True)
            if view.owned_by_id != self.actor.id:
                fail(403, "forbidden", "Only the owner can edit this View.")
            if view.is_locked:
                fail(409, "view_locked", "This View is locked.")
            check_revision(self.request, view.revision)
            if not managed(view):
                fail(400, "unsupported_view", "Update legacy Views through the existing native editor.")
            definition = clean_definition(payload, current=view)
            self.check_semantics(definition)
            for key, value in definition.items():
                setattr(view, key, value)
            view.revision += 1
            view.updated_by = self.actor
            view.save(disable_auto_set_user=True)
            return view

    def delete(self, view_id):
        with transaction.atomic():
            view = self.get(view_id, lock=True)
            if view.owned_by_id != self.actor.id and self.member.role != 20:
                fail(403, "forbidden", "Only the owner or project admin can delete this View.")
            check_revision(self.request, view.revision)
            # Match native deletion semantics: a lock prevents edits, not owner/admin deletion.
            view.delete()
            UserFavorite.objects.filter(project=self.project, entity_identifier=view.id, entity_type="view").delete()
            UserRecentVisit.objects.filter(project=self.project, entity_identifier=view.id, entity_name="view").delete(
                soft=False
            )

    def list(self, params):
        validate_params(params, {"cursor", "per_page", "name"})
        per_page = page_size(params.get("per_page", 50))
        queryset = self.queryset().order_by("id")
        if "name" in params:
            queryset = queryset.filter(name=params["name"])
        scope = {
            "actor": str(self.actor.id),
            "project": str(self.project.id),
            "kind": "views",
            "name": params.get("name"),
        }
        if params.get("cursor"):
            cursor = decode_cursor(params["cursor"], scope)
            queryset = queryset.filter(id__gt=cursor["last_id"])
        rows = list(queryset[: per_page + 1])
        more = len(rows) > per_page
        rows = rows[:per_page]
        cursor = signing.dumps({**scope, "last_id": str(rows[-1].id)}, salt="project-views-v1") if more else None
        return {"results": [self.serialize(row) for row in rows], "next_cursor": cursor, "next_page_results": more}

    def evaluate(self, definition, params, view=None):
        validate_params(params, {"partition", "per_page", "cursor"})
        days = self.check_semantics(definition)
        display = display_filters(definition.get("display_filters", {}))
        partition = params.get("partition", "matches")
        if partition not in {"matches", "needs_review"}:
            fail(400, "validation_error", "partition must be matches or needs_review.")
        per_page = page_size(params.get("per_page", 50))
        scope = {
            "actor": str(self.actor.id),
            "project": str(self.project.id),
            "kind": "work_items",
            "view": str(view.id) if view else None,
            "revision": view.revision if view else None,
            "definition": digest(
                {
                    "rich_filters": definition["rich_filters"],
                    "filter_meta": definition["filter_meta"],
                    "display_filters": display,
                }
            ),
            "partition": partition,
            "policy": POLICY if days is not None else None,
        }
        cursor = decode_cursor(params["cursor"], scope) if params.get("cursor") else None
        evaluated_at = parse_datetime(cursor["evaluated_at"]) if cursor else timezone.now()
        if evaluated_at is None or timezone.is_naive(evaluated_at):
            fail(400, "invalid_cursor", "Invalid evaluation timestamp.")
        queryset = Issue.all_objects.filter(
            project=self.project, workspace=self.member.workspace, deleted_at__isnull=True
        )
        if self.member.role == 5 and not self.project.guest_view_all_features:
            queryset = queryset.filter(created_by=self.actor)
        if not display["sub_issue"]:
            queryset = queryset.filter(parent__isnull=True)
        queryset = apply_ordinary_filters(queryset, definition["rich_filters"])
        if days is not None:
            from plane.utils.meaningful_activity import annotate_activity

            queryset = annotate_activity(
                queryset,
                evaluated_at=evaluated_at,
                days=days,
                policy_version=POLICY,
                source_schema_version=SOURCE_SCHEMA,
            )
        else:
            queryset = queryset.annotate(
                activity_match=Value(True, output_field=BooleanField()),
                last_meaningful_at=Value(None, output_field=DateTimeField()),
                activity_reason=Value("filter_match"),
            )
        matches = queryset.filter(activity_match=True)
        unknown = queryset.filter(activity_match__isnull=True)
        observed_at = timezone.now()
        # Count identities, avoiding DISTINCT over task text and all derived columns.
        counts = {"matches": matches.values("pk").count(), "needs_review": unknown.values("pk").count()}
        selected = matches if partition == "matches" else unknown
        ordering = display["order_by"]
        field = ordering.lstrip("-")
        descending = ordering.startswith("-")
        if cursor:
            lookup = "lt" if descending else "gt"
            if field == "id":
                selected = selected.filter(**{f"id__{lookup}": cursor["last_id"]})
            else:
                selected = selected.filter(
                    Q(**{f"{field}__{lookup}": cursor["last_value"]})
                    | Q(**{field: cursor["last_value"], f"id__{lookup}": cursor["last_id"]})
                )
        selected = (
            selected.order_by(ordering, "-id" if descending else "id")
            .select_related("state")
            .prefetch_related(
                Prefetch("issue_assignee", queryset=IssueAssignee.objects.all()),
                Prefetch("label_issue", queryset=IssueLabel.objects.all()),
                Prefetch("issue_module", queryset=ModuleIssue.objects.all()),
            )
        )
        rows = list(selected[: per_page + 1])
        more = len(rows) > per_page
        rows = rows[:per_page]
        next_cursor = None
        if more:
            value = getattr(rows[-1], field)
            next_cursor = signing.dumps(
                {
                    **scope,
                    "evaluated_at": timestamp(evaluated_at),
                    "last_id": str(rows[-1].id),
                    "last_value": timestamp(value) if field == "created_at" else str(value),
                },
                salt="project-views-v1",
            )
        return {
            "results": [serialize_issue(row) for row in rows],
            "next_cursor": next_cursor,
            "next_page_results": more,
            "partition": partition,
            "evaluated_at": timestamp(evaluated_at),
            "cutoff_at": timestamp(evaluated_at - timedelta(days=days)) if days is not None else None,
            "policy_version": POLICY if days is not None else None,
            "source_schema_version": SOURCE_SCHEMA if days is not None else None,
            "view_revision": view.revision if view else None,
            "counts": counts,
            "counts_observed_at": timestamp(observed_at),
            "assessment_complete": counts["needs_review"] == 0,
            "consistency": "live",
            "counts_consistency": "live",
        }


def validate_params(params, allowed):
    if set(params) - allowed:
        fail(400, "validation_error", "Unknown parameters.")


def page_size(value):
    if type(value) is bool or not re.fullmatch(r"[1-9][0-9]{0,2}", str(value)) or not 1 <= int(value) <= 100:
        fail(400, "validation_error", "per_page must be an integer from 1 to 100.")
    return int(value)


def decode_cursor(token, scope):
    try:
        value = signing.loads(token, salt="project-views-v1", max_age=86400)
        if not isinstance(value, dict) or any(value.get(key) != expected for key, expected in scope.items()):
            raise ValueError
        UUID(value["last_id"])
        return value
    except (signing.BadSignature, ValueError, TypeError, KeyError):
        fail(400, "invalid_cursor", "Cursor is invalid, expired, or belongs to a changed definition/context.")


def serialize_issue(issue):
    result = {
        key: getattr(issue, key)
        for key in (
            "id",
            "name",
            "state_id",
            "project_id",
            "priority",
            "sequence_id",
            "sort_order",
            "created_at",
            "updated_at",
            "start_date",
            "target_date",
            "parent_id",
            "is_draft",
            "archived_at",
            "completed_at",
        )
    }
    result.update(
        assignee_ids=[row.assignee_id for row in issue.issue_assignee.all()],
        label_ids=[row.label_id for row in issue.label_issue.all()],
        module_ids=[row.module_id for row in issue.issue_module.all()],
        last_meaningful_at=timestamp(issue.last_meaningful_at),
        activity_reason=issue.activity_reason,
        activity={
            "match": issue.activity_match,
            "last_meaningful_at": timestamp(issue.last_meaningful_at),
            "reason": issue.activity_reason,
        },
    )
    result["state__group"] = issue.state.group if issue.state else None
    return result
