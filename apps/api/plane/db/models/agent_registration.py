# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

import uuid

from django.db import models


class ProjectAgentRegistration(models.Model):
    """A stable project/runtime slot, independent of users and chat lifetimes.

    This deliberately does not inherit soft deletion. Deactivation preserves
    the unique slot and its revision so an old writer cannot recreate it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey("db.Project", on_delete=models.CASCADE, related_name="agent_registrations")
    runtime = models.CharField(max_length=16, choices=(("codex", "Codex"), ("hermes", "Hermes")))
    agent_key = models.CharField(max_length=64)
    display_name = models.CharField(max_length=255)
    binding = models.JSONField(default=dict)
    setup = models.JSONField(default=dict)
    active = models.BooleanField(default=True)
    revision = models.PositiveBigIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "project_agent_registrations"
        ordering = ("runtime", "agent_key")
        constraints = [
            models.UniqueConstraint(
                fields=["project", "runtime", "agent_key"],
                name="project_agent_registration_unique_slot",
            )
        ]
