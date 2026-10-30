"""Explicit assignment routing and durable single-owner execution state."""

import uuid

from django.db import models


class ProjectAssignmentRoute(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE, related_name="assignment_routes")
    user = models.ForeignKey("db.User", on_delete=models.PROTECT, related_name="assignment_routes")
    contact = models.ForeignKey("db.ProjectTeamContact", on_delete=models.PROTECT, related_name="assignment_routes")
    active = models.BooleanField(default=True)
    revision = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project_assignment_routes"
        ordering = ("user_id",)
        constraints = [models.UniqueConstraint(fields=("project", "user"), name="project_assignment_route_unique_user")]


class ProjectDispatchServiceGrant(models.Model):
    """Public verification material only; the dispatcher owns its private key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE, related_name="dispatch_service_grants")
    user = models.ForeignKey("db.User", on_delete=models.PROTECT, related_name="dispatch_service_grants")
    sender_address = models.CharField(max_length=128)
    public_key = models.CharField(max_length=44)
    active = models.BooleanField(default=True)
    revision = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project_dispatch_service_grants"
        ordering = ("sender_address", "id")
        constraints = [
            models.UniqueConstraint(fields=("project", "sender_address"), name="project_dispatch_grant_unique_sender")
        ]


class WorkItemExecution(models.Model):
    """One durable owner; no lease expiry can transfer a running execution."""

    issue = models.OneToOneField("db.Issue", primary_key=True, on_delete=models.CASCADE, related_name="agent_execution")
    registration = models.ForeignKey(
        "db.ProjectAgentRegistration", on_delete=models.PROTECT, null=True, related_name="work_item_executions"
    )
    run_id = models.CharField(max_length=128, null=True)
    request_id = models.CharField(max_length=128, null=True)
    status = models.CharField(
        max_length=16,
        default="idle",
        choices=[
            (value, value) for value in ("idle", "running", "awaiting", "paused", "completed", "failed", "stopped")
        ],
    )
    checkpoint = models.JSONField(default=dict)
    revision = models.PositiveBigIntegerField(default=1)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "work_item_executions"


class WorkItemExecutionClaim(models.Model):
    """Retain request/run identities so a delayed old dispatch cannot rerun work."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    issue = models.ForeignKey("db.Issue", on_delete=models.CASCADE, related_name="agent_execution_claims")
    registration = models.ForeignKey("db.ProjectAgentRegistration", on_delete=models.PROTECT)
    run_id = models.CharField(max_length=128)
    request_id = models.CharField(max_length=128)
    claimed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "work_item_execution_claims"
        constraints = [
            models.UniqueConstraint(fields=("issue", "run_id"), name="work_item_execution_unique_run"),
            models.UniqueConstraint(fields=("issue", "request_id"), name="work_item_execution_unique_request"),
        ]
