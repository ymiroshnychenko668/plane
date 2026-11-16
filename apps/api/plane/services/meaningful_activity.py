"""Attributable, revisioned assessment of captured task text; no task mutations."""

import re
from uuid import UUID

from django.core import signing
from django.db import transaction
from django.db.models import Max, Prefetch, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from plane.db.models import (
    ActivityAssessment,
    ActivityAssessmentReceipt,
    ActivityAssessmentRevision,
    ActivityCoverage,
    ActivitySourceEvent,
    Issue,
)
from plane.services.views import ProjectViews, digest, fail, page_size, timestamp, validate_params
from plane.utils.meaningful_activity import (
    POLICY,
    SOURCE_SCHEMA,
    assessed_events,
    capture_available,
    pending_events,
    visible_events,
)

CATEGORIES = {
    "meaningful": {"implementation", "investigation", "verification", "decision", "blocker"},
    "routine": {"routine"},
    "needs_review": {"unknown"},
}


def policy(params, required=True):
    value = params.get("policy_version")
    if value is None and not required:
        return POLICY
    if value != POLICY:
        fail(400, "unsupported_policy", f"policy_version must be {POLICY}.")
    return value


def revision(request):
    value = request.headers.get("If-Match")
    if value is None:
        fail(428, "precondition_required", "If-Match is required for assessments.")
    matched = re.fullmatch(r'(?:W/)?"(0|[1-9][0-9]{0,18})"', value.strip())
    if not matched:
        fail(400, "validation_error", "Expected one quoted nonnegative numeric revision.")
    return int(matched.group(1))


def text_field(payload, key):
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        fail(400, "validation_error", f"{key} must contain 1 to 4096 characters.")
    return value.strip()


def request_id(payload):
    value = payload.get("request_id")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", value):
        fail(400, "validation_error", "request_id must be an opaque 8 to 128 character identifier.")
    return value


class ProjectActivity:
    def __init__(self, request, slug, project_id):
        context = ProjectViews(request, slug, project_id)
        if context.member.role < 15:
            fail(403, "activity_unavailable", "Activity assessments require full project source visibility.")
        self.request = request
        self.actor = context.actor
        self.project = context.project

    def issue(self, issue_id, lock=False):
        queryset = Issue.all_objects.filter(project=self.project, deleted_at__isnull=True)
        if lock:
            # Source capture uses the same issue-first lock order.
            queryset = queryset.select_for_update(no_key=True)
        value = queryset.filter(pk=issue_id).first()
        if value is None:
            fail(404, "not_found", "Work item unavailable.")
        return value

    def events(self, issue=None):
        result = visible_events(ActivitySourceEvent.objects.filter(project=self.project)).filter(
            source_schema_version=SOURCE_SCHEMA,
            is_baseline=False,
        )
        if issue is not None:
            result = result.filter(issue=issue)
        return result

    def assessment(self, issue, assessment_id):
        result = (
            ActivityAssessment.objects.filter(
                id=assessment_id,
                policy_version=POLICY,
                event_id__in=self.events(issue).values("id"),
            )
            .select_related("event")
            .first()
        )
        if result is None:
            fail(404, "not_found", "Assessment unavailable.")
        return result

    def get(self, issue_id, assessment_id, params):
        validate_params(params, {"policy_version"})
        policy(params, required=False)
        return self.assessment(self.issue(issue_id), assessment_id)

    def summary(self, issue_id, params):
        validate_params(params, {"policy_version"})
        policy(params)
        issue = self.issue(issue_id)
        available = capture_available()
        coverage = ActivityCoverage.objects.filter(issue=issue, source_schema_version=SOURCE_SCHEMA).first()
        last = (
            assessed_events(POLICY)
            .filter(
                issue=issue,
                activity_decision="meaningful",
                time_quality="native",
                occurred_at__lte=timezone.now(),
            )
            .aggregate(last=Max("occurred_at"))["last"]
        )
        pending = pending_events(POLICY).filter(project=self.project, issue=issue).count()
        return {
            "issue_id": str(issue.id),
            "project_id": str(self.project.id),
            "last_meaningful_at": timestamp(last),
            "capture_started_at": timestamp(coverage.captured_from) if coverage and available else None,
            "known_existing_at": timestamp(coverage.known_existing_at) if coverage else None,
            "pending_count": pending,
            "capture_available": available,
            "policy_version": POLICY,
            "source_schema_version": SOURCE_SCHEMA,
            "observed_at": timestamp(timezone.now()),
            "consistency": "live",
        }

    def candidates(self, params, issue_id=None):
        validate_params(params, {"policy_version", "cursor", "per_page"})
        policy(params)
        issue = self.issue(issue_id) if issue_id is not None else None
        queryset = self.events(issue)
        if issue is None:
            queryset = queryset.filter(id__in=pending_events(POLICY).values("id"))
        scope = {
            "actor": str(self.actor.id),
            "project": str(self.project.id),
            "issue": str(issue.id) if issue else None,
            "policy": POLICY,
            "kind": "candidates" if issue else "pending",
        }
        cursor = params.get("cursor")
        if cursor:
            try:
                decoded = signing.loads(cursor, salt="activity-candidates-v1", max_age=86400)
                if not isinstance(decoded, dict) or any(decoded.get(key) != value for key, value in scope.items()):
                    raise ValueError
                last_id = UUID(decoded["last_id"])
                observed = parse_datetime(decoded["last_observed"])
                if observed is None or timezone.is_naive(observed):
                    raise ValueError
            except (ValueError, KeyError, TypeError, signing.BadSignature):
                fail(400, "invalid_cursor", "Cursor is invalid, expired, or belongs to another context.")
            queryset = queryset.filter(Q(observed_at__gt=observed) | Q(observed_at=observed, id__gt=last_id))
        limit = page_size(params.get("per_page", 50))
        rows = list(
            queryset.order_by("observed_at", "id").prefetch_related(
                Prefetch("assessments", queryset=ActivityAssessment.objects.filter(policy_version=POLICY)),
            )[: limit + 1]
        )
        more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            signing.dumps(
                {
                    **scope,
                    "last_observed": timestamp(rows[-1].observed_at),
                    "last_id": str(rows[-1].id),
                },
                salt="activity-candidates-v1",
            )
            if more
            else None
        )
        return {
            "results": [serialize_event(event) for event in rows],
            "next_page_results": more,
            "next_cursor": next_cursor,
            "policy_version": POLICY,
            "source_schema_version": SOURCE_SCHEMA,
            "observed_at": timestamp(timezone.now()),
            "consistency": "live",
        }

    def replay(self, issue, identifier, intent_hash):
        receipt = ActivityAssessmentReceipt.objects.filter(issue=issue, actor=self.actor, request_id=identifier).first()
        if receipt is None:
            return None
        if receipt.intent_hash != intent_hash:
            fail(409, "request_id_conflict", "request_id was used for another assessment operation.")
        current = self.assessment(issue, receipt.assessment_id)
        if current.revision != receipt.revision:
            fail(
                409,
                "request_result_changed",
                "The previous operation succeeded, but its result has since changed. Read it before retrying.",
            )
        return current

    def save_revision(self, issue, assessment, identifier, intent_hash):
        ActivityAssessmentRevision.objects.create(
            assessment=assessment,
            revision=assessment.revision,
            decision=assessment.decision,
            category=assessment.category,
            rationale=assessment.rationale,
            assessor=self.actor,
            assessed_at=assessment.assessed_at,
        )
        ActivityAssessmentReceipt.objects.create(
            issue=issue,
            actor=self.actor,
            request_id=identifier,
            intent_hash=intent_hash,
            assessment=assessment,
            revision=assessment.revision,
        )

    def assess(self, issue_id, payload):
        expected = revision(self.request)
        allowed = {"event_id", "source_revision", "policy_version", "decision", "category", "rationale", "request_id"}
        if not isinstance(payload, dict) or set(payload) != allowed:
            fail(400, "validation_error", "Assessment fields must exactly match the supported schema.")
        policy(payload)
        identifier = request_id(payload)
        rationale = text_field(payload, "rationale")
        try:
            event_id = UUID(payload["event_id"])
        except (ValueError, TypeError, AttributeError):
            fail(400, "validation_error", "event_id must be a UUID.")
        if type(payload["source_revision"]) is not int or payload["source_revision"] < 1:
            fail(400, "validation_error", "source_revision must be a positive integer.")
        decision = payload["decision"]
        category = payload["category"]
        if (
            not isinstance(decision, str)
            or decision not in CATEGORIES
            or not isinstance(category, str)
            or category not in CATEGORIES[decision]
        ):
            fail(400, "validation_error", "Decision and category are inconsistent or unsupported.")
        intent = digest({"action": "assess", "expected_revision": expected, "payload": payload})
        with transaction.atomic():
            issue = self.issue(issue_id, lock=True)
            previous = self.replay(issue, identifier, intent)
            if previous:
                return previous, False
            event = self.events(issue).select_for_update().filter(pk=event_id).first()
            if event is None:
                fail(404, "not_found", "Source event unavailable.")
            if event.source_revision != payload["source_revision"]:
                fail(409, "source_revision_conflict", "The selected source revision does not match.")
            assessment = (
                ActivityAssessment.objects.select_for_update().filter(event=event, policy_version=POLICY).first()
            )
            if expected != (assessment.revision if assessment else 0):
                fail(412, "revision_conflict", "The assessment changed. Read the current decision before retrying.")
            created = assessment is None
            if created:
                assessment = ActivityAssessment(event=event, policy_version=POLICY)
            else:
                assessment.revision += 1
            assessment.decision = decision
            assessment.category = category
            assessment.rationale = rationale
            assessment.assessor = self.actor
            assessment.assessed_at = timezone.now()
            assessment.save()
            self.save_revision(issue, assessment, identifier, intent)
            return assessment, created

    def retract(self, issue_id, assessment_id, payload):
        expected = revision(self.request)
        if not isinstance(payload, dict) or set(payload) != {"request_id", "reason"}:
            fail(400, "validation_error", "Retraction requires request_id and reason only.")
        identifier = request_id(payload)
        reason = text_field(payload, "reason")
        intent = digest(
            {
                "action": "retract",
                "assessment_id": str(assessment_id),
                "expected_revision": expected,
                "payload": payload,
            }
        )
        with transaction.atomic():
            issue = self.issue(issue_id, lock=True)
            previous = self.replay(issue, identifier, intent)
            if previous:
                return previous
            assessment = self.assessment(issue, assessment_id)
            if expected != assessment.revision:
                fail(412, "revision_conflict", "The assessment changed. Read its current decision before retracting.")
            assessment.revision += 1
            assessment.decision = "needs_review"
            assessment.category = "unknown"
            assessment.rationale = reason
            assessment.assessor = self.actor
            assessment.assessed_at = timezone.now()
            assessment.save()
            self.save_revision(issue, assessment, identifier, intent)
            return assessment


def serialize_assessment(value):
    return {
        "id": str(value.id),
        "event_id": str(value.event_id),
        "revision": value.revision,
        "policy_version": value.policy_version,
        "decision": value.decision,
        "category": value.category,
        "rationale": value.rationale,
        "assessor_id": str(value.assessor_id) if value.assessor_id else None,
        "assessed_at": timestamp(value.assessed_at),
    }


def serialize_event(event):
    assessment = next(iter(event.assessments.all()), None)
    return {
        "id": str(event.id),
        "event_id": str(event.id),
        "issue_id": str(event.issue_id),
        "project_id": str(event.project_id),
        "source_revision": event.source_revision,
        "source_kind": event.source_kind,
        "source_id": str(event.source_id),
        "source_content": event.source_content,
        "before_content": event.before_content,
        "occurred_at": timestamp(event.occurred_at),
        "observed_at": timestamp(event.observed_at),
        "time_quality": event.time_quality,
        "is_baseline": event.is_baseline,
        "source_schema_version": event.source_schema_version,
        "assessment": serialize_assessment(assessment) if assessment else None,
    }
