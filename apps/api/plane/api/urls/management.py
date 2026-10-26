# Copyright (c) 2023-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

from django.urls import path

from plane.api.views.agent_registration import ProjectAgentRegistrationAPIEndpoint
from plane.api.views.management import (
    ManagementCapabilitiesAPIEndpoint,
    PageContentAPIEndpoint,
    PageDuplicateAPIEndpoint,
    PageFavoriteAPIEndpoint,
    PageManagementViewSet,
    PageVersionAPIEndpoint,
    WebhookLogsAPIEndpoint,
    WebhookManagementAPIEndpoint,
    WebhookSecretAPIEndpoint,
    WorkspaceManagementViewSet,
    WorkspaceMemberManagementViewSet,
    WorkItemArchiveAPIEndpoint,
    WorkItemRelationDetailAPIEndpoint,
)

workspace = "workspaces/<str:slug>/"
project = workspace + "projects/<uuid:project_id>/"
page = project + "pages/<uuid:page_id>/"

urlpatterns = [
    path("workspaces/", WorkspaceManagementViewSet.as_view({"get": "list", "post": "create"})),
    path(
        workspace,
        WorkspaceManagementViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path(workspace + "management-capabilities/", ManagementCapabilitiesAPIEndpoint.as_view()),
    path(workspace + "workspace-members/", WorkspaceMemberManagementViewSet.as_view({"get": "list", "post": "create"})),
    path(
        workspace + "workspace-members/<uuid:pk>/",
        WorkspaceMemberManagementViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
    ),
    path(project + "pages/", PageManagementViewSet.as_view({"get": "list", "post": "create"})),
    path(project + "agent-registrations/", ProjectAgentRegistrationAPIEndpoint.as_view()),
    path(
        project + "agent-registrations/<str:runtime>/<str:agent_key>/",
        ProjectAgentRegistrationAPIEndpoint.as_view(),
    ),
    path(project + "pages-summary/", PageManagementViewSet.as_view({"get": "summary"})),
    path(page, PageManagementViewSet.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"})),
    path(page + "archive/", PageManagementViewSet.as_view({"post": "archive", "delete": "unarchive"})),
    path(page + "lock/", PageManagementViewSet.as_view({"post": "lock", "delete": "unlock"})),
    path(page + "access/", PageManagementViewSet.as_view({"post": "access"})),
    path(page + "description/", PageContentAPIEndpoint.as_view({"get": "retrieve", "patch": "partial_update"})),
    path(page + "versions/", PageVersionAPIEndpoint.as_view()),
    path(page + "versions/<uuid:pk>/", PageVersionAPIEndpoint.as_view()),
    path(page + "duplicate/", PageDuplicateAPIEndpoint.as_view()),
    path(
        project + "favorite-pages/<uuid:page_id>/",
        PageFavoriteAPIEndpoint.as_view({"post": "create", "delete": "destroy"}),
    ),
    path(workspace + "webhooks/", WebhookManagementAPIEndpoint.as_view(http_method_names=["get", "post"])),
    path(
        workspace + "webhooks/<uuid:pk>/",
        WebhookManagementAPIEndpoint.as_view(http_method_names=["get", "patch", "delete"]),
    ),
    path(workspace + "webhooks/<uuid:pk>/regenerate/", WebhookSecretAPIEndpoint.as_view()),
    path(workspace + "webhook-logs/<uuid:webhook_id>/", WebhookLogsAPIEndpoint.as_view()),
    path(project + "archived-work-items/", WorkItemArchiveAPIEndpoint.as_view({"get": "list"})),
    path(
        project + "work-items/<uuid:pk>/archive/",
        WorkItemArchiveAPIEndpoint.as_view({"get": "retrieve", "post": "archive", "delete": "unarchive"}),
    ),
    path(project + "work-items/<uuid:issue_id>/relations/<uuid:pk>/", WorkItemRelationDetailAPIEndpoint.as_view()),
]
