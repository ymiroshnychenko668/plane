"""Strict, versioned project View grammar. Never reuse generic filter fallbacks."""

import json
from uuid import UUID

from django.db.models import Exists, OuterRef
from rest_framework.exceptions import ValidationError

ACTIVITY = "meaningful_activity__older_than_days"
UNASSIGNED = "assignee_id__is_empty"
POLICY = "meaningful_activity_v1"
SOURCE_SCHEMA = "task_text_v1"
MAX_CONDITIONS = 32
MAX_FILTER_BYTES = 32768
UUID_FIELDS = {"state_id", "assignee_id", "label_id", "module_id", "cycle_id", "created_by_id"}
ENUMS = {
    "state_group": {"backlog", "unstarted", "started", "completed", "cancelled"},
    "priority": {"urgent", "high", "medium", "low", "none"},
}
SPECIAL_FIELDS = {ACTIVITY, UNASSIGNED}


def invalid(detail):
    raise ValidationError({"code": "unsupported_filter", "detail": detail})


def reject_managed_predicates(value):
    """Reject nested predicates before a generic compiler can discard them."""
    if isinstance(value, dict):
        for key, child in value.items():
            if (
                key in SPECIAL_FIELDS
                or key.startswith("meaningful_activity__")
                or key.startswith("assignee_id__is_empty")
            ):
                invalid("This predicate is available only through project View endpoints.")
            if key in {"filters", "rich_filters"} and isinstance(child, str):
                try:
                    child = json.loads(child)
                except (ValueError, TypeError):
                    pass
            reject_managed_predicates(child)
    elif isinstance(value, list):
        for child in value:
            reject_managed_predicates(child)


def validate_definition(rich_filters, filter_meta):
    if not isinstance(rich_filters, dict) or set(rich_filters) != {"and"}:
        invalid("Version 1 requires one AND group.")
    if len(json.dumps(rich_filters, separators=(",", ":")).encode()) > MAX_FILTER_BYTES:
        invalid("Filter exceeds 32 KiB.")
    leaves = rich_filters["and"]
    if not isinstance(leaves, list) or not 1 <= len(leaves) <= MAX_CONDITIONS:
        invalid("Provide 1 to 32 conditions.")
    seen = set()
    days = None
    for leaf in leaves:
        if not isinstance(leaf, dict) or len(leaf) != 1:
            invalid("Each condition must contain exactly one supported predicate.")
        key, value = next(iter(leaf.items()))
        if key in seen:
            invalid("Duplicate predicates are not supported.")
        seen.add(key)
        if key == ACTIVITY:
            if type(value) is not int or not 1 <= value <= 36500:
                invalid("Activity days must be an integer from 1 to 36500.")
            days = value
        elif key == UNASSIGNED:
            if value is not True:
                invalid("assignee_id__is_empty accepts true only.")
        elif key in {"is_archived__exact", "is_draft__exact"}:
            if type(value) is not bool:
                invalid("Boolean predicates require JSON booleans.")
        else:
            parts = key.rsplit("__", 1)
            if len(parts) != 2 or parts[0] not in UUID_FIELDS | set(ENUMS) or parts[1] not in {"exact", "in"}:
                invalid(f"Unsupported predicate: {key}.")
            field, operator = parts
            if not isinstance(value, str) or not value or value.strip() != value:
                invalid("Values must be nonempty canonical strings; __in uses CSV.")
            values = value.split(",") if operator == "in" else [value]
            if len(values) > 100 or len(set(values)) != len(values):
                invalid("CSV values must be unique and contain at most 100 entries.")
            for item in values:
                if field in UUID_FIELDS:
                    try:
                        if str(UUID(item)) != item:
                            raise ValueError
                    except (ValueError, TypeError, AttributeError):
                        invalid("IDs must be canonical UUID strings.")
                elif item not in ENUMS[field]:
                    invalid(f"Unsupported value for {field}.")
    expected_meta = {"version": 1}
    if days is not None:
        expected_meta.update(policy_version=POLICY, source_schema_version=SOURCE_SCHEMA)
    if not isinstance(filter_meta, dict) or filter_meta != expected_meta or type(filter_meta.get("version")) is not int:
        invalid(f"filter_meta must equal {expected_meta}.")
    return days


def apply_ordinary_filters(queryset, rich_filters):
    from plane.db.models import IssueAssignee
    from plane.utils.filters.filterset import IssueFilterSet

    for leaf in rich_filters["and"]:
        key, value = next(iter(leaf.items()))
        if key == ACTIVITY:
            continue
        if key == UNASSIGNED:
            queryset = queryset.annotate(
                _view_has_assignee=Exists(IssueAssignee.objects.filter(issue_id=OuterRef("pk")))
            ).filter(_view_has_assignee=False)
        else:
            bound = IssueFilterSet(data={key: value}, queryset=queryset)
            if not bound.is_valid():
                invalid("Invalid ordinary predicate.")
            queryset = queryset.filter(bound.build_combined_q())
    return queryset.distinct()
