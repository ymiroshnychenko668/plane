from django.urls import path
from plane.api.views.views import ProjectViewAPIEndpoint, ProjectViewPreviewAPIEndpoint, ProjectViewWorkItemsAPIEndpoint

urlpatterns = [
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/views/",
        ProjectViewAPIEndpoint.as_view(http_method_names=["get", "post", "head", "options"]),
        name="api-project-views",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/views/preview/",
        ProjectViewPreviewAPIEndpoint.as_view(),
        name="api-project-view-preview",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/views/<uuid:pk>/",
        ProjectViewAPIEndpoint.as_view(http_method_names=["get", "patch", "delete", "head", "options"]),
        name="api-project-view-detail",
    ),
    path(
        "workspaces/<str:slug>/projects/<uuid:project_id>/views/<uuid:pk>/work-items/",
        ProjectViewWorkItemsAPIEndpoint.as_view(),
        name="api-project-view-work-items",
    ),
]
