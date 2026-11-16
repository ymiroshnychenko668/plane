"""Recorded source revisions and attributable assessments, separate from execution."""

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone


class ActivityCoverage(models.Model):
    issue = models.OneToOneField(
        "db.Issue", primary_key=True, on_delete=models.CASCADE, related_name="activity_coverage"
    )
    captured_from = models.DateTimeField(default=timezone.now)
    known_existing_at = models.DateTimeField(default=timezone.now)
    native_created_at = models.DateTimeField(null=True)
    generation = models.PositiveBigIntegerField(default=0)
    source_schema_version = models.CharField(max_length=32, default="task_text_v1")

    class Meta:
        db_table = "activity_coverage"


class ActivitySourceEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey("db.Issue", on_delete=models.CASCADE, related_name="activity_source_events")
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE)
    source_kind = models.CharField(max_length=32)
    source_id = models.UUIDField()
    source_revision = models.PositiveBigIntegerField()
    capture_transaction = models.BigIntegerField()
    mutation_id = models.CharField(max_length=128, blank=True)
    authenticated_writer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    source_content = models.JSONField(default=dict)
    before_content = models.JSONField(default=dict)
    occurred_at = models.DateTimeField(null=True)
    observed_at = models.DateTimeField(default=timezone.now)
    time_quality = models.CharField(max_length=24, default="unattributed")
    is_baseline = models.BooleanField(default=False)
    redacted = models.BooleanField(default=False)
    source_schema_version = models.CharField(max_length=32, default="task_text_v1")

    class Meta:
        db_table = "activity_source_events"
        ordering = ("observed_at", "id")
        constraints = [
            models.UniqueConstraint(
                fields=("source_kind", "source_id", "capture_transaction"), name="activity_source_transaction"
            )
        ]
        indexes = [
            models.Index(fields=("issue", "observed_at"), name="activity_event_issue_time"),
            models.Index(fields=("project", "observed_at", "id"), name="activity_event_project_queue"),
            models.Index(fields=("source_kind", "source_id"), name="activity_event_source"),
        ]


class ActivityAssessment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(ActivitySourceEvent, on_delete=models.CASCADE, related_name="assessments")
    policy_version = models.CharField(max_length=48, default="meaningful_activity_v1")
    decision = models.CharField(max_length=16)
    category = models.CharField(max_length=24)
    rationale = models.TextField(blank=True)
    assessor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    assessed_at = models.DateTimeField(default=timezone.now)
    revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "activity_assessments"
        constraints = [models.UniqueConstraint(fields=("event", "policy_version"), name="activity_event_policy")]


class ActivityAssessmentRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(ActivityAssessment, on_delete=models.CASCADE, related_name="history")
    revision = models.PositiveBigIntegerField()
    decision = models.CharField(max_length=16)
    category = models.CharField(max_length=24)
    rationale = models.TextField(blank=True)
    assessor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    assessed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "activity_assessment_revisions"
        constraints = [models.UniqueConstraint(fields=("assessment", "revision"), name="activity_assessment_revision")]


class ActivityAssessmentReceipt(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey("db.Issue", on_delete=models.CASCADE)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    request_id = models.CharField(max_length=128)
    intent_hash = models.CharField(max_length=64)
    assessment = models.ForeignKey(ActivityAssessment, on_delete=models.CASCADE)
    revision = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "activity_assessment_receipts"
        constraints = [
            models.UniqueConstraint(fields=("issue", "actor", "request_id"), name="activity_assessment_request")
        ]
