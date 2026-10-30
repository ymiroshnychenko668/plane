# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Project work assigned to the authenticated API user."""

from rest_framework.exceptions import ValidationError

from plane.api.serializers import IssueSerializer
from plane.app.permissions import ProjectEntityPermission
from plane.db.models import Issue, ProjectMember
from plane.db.models.state import StateGroup

from .base import BaseAPIView
from .management import APIKeyManagementMixin


class MyWorkItemsAPIEndpoint(APIKeyManagementMixin, BaseAPIView):
    permission_classes = [ProjectEntityPermission]
    http_method_names = ["get", "head", "options"]

    def get(self, request, slug, project_id):
        allowed = {"state_group", "cursor", "per_page", "fields", "expand"}
        unsupported = set(request.query_params) - allowed
        if unsupported:
            raise ValidationError({"unsupported_parameters": sorted(unsupported)})
        groups = None
        if "state_group" in request.query_params:
            groups = request.query_params["state_group"].split(",")
            supported = set(StateGroup.values) - {StateGroup.TRIAGE.value}
            if not groups or any(group not in supported for group in groups):
                raise ValidationError({"state_group": "Use comma-separated work item state groups."})

        issues = (
            Issue.issue_objects.filter(
                workspace__slug=slug,
                project_id=project_id,
                issue_assignee__assignee=request.user,
                issue_assignee__deleted_at__isnull=True,
            )
            .select_related("project", "workspace", "state", "parent")
            .prefetch_related("assignees", "labels")
            .order_by("created_at", "id")
            .distinct()
        )
        if ProjectMember.objects.filter(
            workspace__slug=slug,
            project_id=project_id,
            member=request.user,
            is_active=True,
            role=5,
            project__guest_view_all_features=False,
        ).exists():
            issues = issues.filter(created_by=request.user)
        if groups is not None:
            issues = issues.filter(state__group__in=groups)
        return self.paginate(
            request=request,
            queryset=issues,
            total_count_queryset=issues,
            on_results=lambda items: (
                IssueSerializer(items, many=True, fields=self.fields, expand=self.expand or ["state"]).data
            ),
        )
