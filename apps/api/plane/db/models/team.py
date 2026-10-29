"""Project transport contacts; memberships and agent identities remain authoritative."""

import uuid

from django.db import models


class ProjectTeamContact(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE, related_name="team_contacts")
    registration = models.ForeignKey(
        "db.ProjectAgentRegistration", on_delete=models.PROTECT, related_name="team_contacts"
    )
    address = models.CharField(max_length=128)
    target = models.JSONField(default=dict)
    active = models.BooleanField(default=True)
    revision = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project_team_contacts"
        ordering = ("address",)
        constraints = [
            models.UniqueConstraint(fields=("project", "address"), name="project_team_contact_unique_address")
        ]


class ProjectTeamMigration(models.Model):
    """Import receipt, never a second copy of the address book."""

    project = models.OneToOneField(
        "db.Project", primary_key=True, on_delete=models.CASCADE, related_name="team_migration"
    )
    page_id = models.UUIDField(unique=True)
    source_revision = models.CharField(max_length=64)
    imported_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "project_team_migrations"
