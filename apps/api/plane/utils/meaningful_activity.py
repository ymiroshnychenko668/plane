"""Explainable, three-valued activity evaluation; no writes or model calls on reads."""

from datetime import timedelta

from django.db import connection
from django.db.models import BooleanField, Case, CharField, DateTimeField, Exists, F, OuterRef, Q, Subquery, Value, When

from plane.db.models import ActivityAssessment, ActivityCoverage, ActivitySourceEvent, IssueComment

POLICY = "meaningful_activity_v1"
SOURCE_SCHEMA = "task_text_v1"


def capture_available():
    """Do not claim continuous capture when required database hooks are absent."""
    if connection.vendor != "postgresql":
        return False
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = current_schema() AND NOT t.tgisinternal AND t.tgenabled IN ('O', 'A') "
            "AND ((c.relname = 'issues' AND t.tgname = 'plane_activity_issue_capture') "
            "OR (c.relname = 'issue_comments' AND t.tgname = 'plane_activity_comment_capture'))"
        )
        return cursor.fetchone()[0] == 2


def visible_events(queryset):
    comments = IssueComment.objects.filter(
        id=OuterRef("source_id"),
        issue_id=OuterRef("issue_id"),
        project_id=OuterRef("project_id"),
        deleted_at__isnull=True,
    )
    return (
        queryset.filter(
            redacted=False,
            issue__deleted_at__isnull=True,
            project_id=F("issue__project_id"),
        )
        .annotate(activity_source_exists=Exists(comments))
        .filter(
            Q(source_kind="description", source_id=F("issue_id"))
            | Q(source_kind="comment", activity_source_exists=True)
        )
    )


def assessed_events(policy_version=POLICY):
    assessment = ActivityAssessment.objects.filter(event_id=OuterRef("pk"), policy_version=policy_version)
    return (
        visible_events(ActivitySourceEvent.objects.all())
        .filter(
            redacted=False,
            is_baseline=False,
            source_schema_version=SOURCE_SCHEMA,
            issue__deleted_at__isnull=True,
        )
        .annotate(activity_decision=Subquery(assessment.values("decision")[:1]))
    )


def pending_events(policy_version=POLICY):
    return assessed_events(policy_version).filter(
        Q(activity_decision__isnull=True) | Q(activity_decision="needs_review")
    )


def annotate_activity(queryset, *, evaluated_at, days, policy_version, source_schema_version):
    if policy_version != POLICY or source_schema_version != SOURCE_SCHEMA:
        raise ValueError("Unsupported meaningful activity policy or source schema.")
    cutoff = evaluated_at - timedelta(days=days)
    coverage = ActivityCoverage.objects.filter(issue_id=OuterRef("pk"), source_schema_version=SOURCE_SCHEMA)
    events = assessed_events(policy_version).filter(issue_id=OuterRef("pk"))
    meaningful = events.filter(
        activity_decision="meaningful",
        time_quality="native",
        occurred_at__lte=evaluated_at,
    ).order_by("-occurred_at")
    # An unknown-time event has an observed upper bound. Once that bound is
    # older than the window, the event cannot make this window incomplete.
    window = events.filter(
        Q(occurred_at__gte=cutoff, occurred_at__lte=evaluated_at) | Q(occurred_at__isnull=True, observed_at__gte=cutoff)
    )
    unresolved = window.filter(
        Q(activity_decision__isnull=True)
        | Q(activity_decision="needs_review")
        | (Q(activity_decision="meaningful") & Q(occurred_at__isnull=True))
    )
    queryset = queryset.annotate(
        last_meaningful_at=Subquery(meaningful.values("occurred_at")[:1], output_field=DateTimeField()),
        activity_capture_started_at=Subquery(coverage.values("captured_from")[:1], output_field=DateTimeField()),
        activity_known_existing_at=Subquery(coverage.values("known_existing_at")[:1], output_field=DateTimeField()),
        activity_native_created_at=Subquery(coverage.values("native_created_at")[:1], output_field=DateTimeField()),
        activity_pending=Exists(unresolved),
    )
    if not capture_available():
        return queryset.annotate(
            activity_match=Value(None, output_field=BooleanField()),
            activity_reason=Value("capture_unavailable", output_field=CharField()),
        )
    queryset = queryset.annotate(
        activity_match=Case(
            When(last_meaningful_at__gte=cutoff, then=Value(False)),
            When(activity_native_created_at__gte=cutoff, then=Value(False)),
            When(activity_capture_started_at__isnull=True, then=Value(None)),
            When(activity_capture_started_at__gt=cutoff, then=Value(None)),
            When(activity_known_existing_at__gte=cutoff, then=Value(None)),
            When(activity_pending=True, then=Value(None)),
            default=Value(True),
            output_field=BooleanField(),
        )
    )
    return queryset.annotate(
        activity_reason=Case(
            When(last_meaningful_at__gte=cutoff, then=Value("recent_meaningful_activity")),
            When(activity_native_created_at__gte=cutoff, then=Value("recent_creation")),
            When(activity_capture_started_at__isnull=True, then=Value("capture_missing")),
            When(activity_capture_started_at__gt=cutoff, then=Value("observation_window_incomplete")),
            When(activity_known_existing_at__gte=cutoff, then=Value("existence_window_incomplete")),
            When(activity_pending=True, then=Value("assessment_incomplete")),
            default=Value("no_meaningful_activity_in_window"),
            output_field=CharField(),
        )
    )
