"""Project-visible infrastructure inventory; never credentials or access grants."""

from django.db import models

from .base import BaseModel


RESOURCE_KINDS = ("physical_host", "vm", "lxc", "service", "database", "storage", "network", "stack")
LIFECYCLES = ("planned", "active", "maintenance", "retired", "unknown")
RELATION_KINDS = ("runs_on", "depends_on")


class InfrastructureResource(BaseModel):
    workspace = models.ForeignKey("db.Workspace", on_delete=models.CASCADE, related_name="infrastructure_resources")
    ownership_project = models.ForeignKey("db.Project", on_delete=models.CASCADE, related_name="owned_infrastructure")
    key = models.CharField(max_length=160)
    name = models.CharField(max_length=255)
    kind = models.CharField(max_length=32, choices=[(value, value) for value in RESOURCE_KINDS])
    lifecycle = models.CharField(max_length=16, default="planned", choices=[(value, value) for value in LIFECYCLES])
    description = models.TextField(default="", blank=True)
    tags = models.JSONField(default=list)
    owner = models.JSONField(default=dict)
    source = models.JSONField(default=dict)
    evidence = models.JSONField(default=list)
    last_verified = models.DateTimeField(null=True, blank=True)
    access_methods = models.JSONField(default=list)
    revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "infrastructure_resources"
        ordering = ("id",)
        constraints = [models.UniqueConstraint(fields=("workspace", "key"), name="infra_resource_workspace_key")]
        indexes = [models.Index(fields=("workspace", "kind", "lifecycle"), name="infra_resource_kind_lifecycle")]


class InfrastructureProjectLink(BaseModel):
    resource = models.ForeignKey(InfrastructureResource, on_delete=models.CASCADE, related_name="project_links")
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE, related_name="infrastructure_links")

    class Meta:
        db_table = "infrastructure_project_links"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("resource", "project"),
                condition=models.Q(deleted_at__isnull=True),
                name="infra_active_project_link",
            )
        ]


class InfrastructureRelation(BaseModel):
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE, related_name="infrastructure_relations")
    source_resource = models.ForeignKey(
        InfrastructureResource, on_delete=models.CASCADE, related_name="outgoing_relations"
    )
    target_resource = models.ForeignKey(
        InfrastructureResource, on_delete=models.CASCADE, related_name="incoming_relations"
    )
    kind = models.CharField(max_length=16, choices=[(value, value) for value in RELATION_KINDS])
    source = models.JSONField(default=dict)
    evidence = models.JSONField(default=list)
    last_verified = models.DateTimeField(null=True, blank=True)
    revision = models.PositiveBigIntegerField(default=1)

    class Meta:
        db_table = "infrastructure_relations"
        ordering = ("id",)
        constraints = [
            models.UniqueConstraint(
                fields=("project", "source_resource", "target_resource", "kind"),
                condition=models.Q(deleted_at__isnull=True),
                name="infra_active_relation",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_resource=models.F("target_resource")), name="infra_no_self_relation"
            ),
        ]
