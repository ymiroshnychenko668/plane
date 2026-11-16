from django.urls import path

from plane.app.views.infrastructure import InfrastructureViewSet


def infrastructure_urls(view, prefix=""):
    base = "workspaces/<str:slug>/projects/<uuid:project_id>/infrastructure/"
    return [
        path(
            base + "resources/",
            view.as_view({"get": "list", "post": "create"}),
            name=prefix + "infrastructure-resources",
        ),
        path(
            base + "resources/<uuid:resource_id>/",
            view.as_view({"get": "retrieve", "patch": "partial_update", "delete": "destroy"}),
            name=prefix + "infrastructure-resource",
        ),
        path(
            base + "relations/",
            view.as_view({"get": "relations_list", "post": "relations_create"}),
            name=prefix + "infrastructure-relations",
        ),
        path(
            base + "relations/<uuid:relation_id>/",
            view.as_view({"delete": "relations_delete"}),
            name=prefix + "infrastructure-relation",
        ),
        path(
            base + "resources/<uuid:resource_id>/project-links/",
            view.as_view({"get": "links_list", "post": "links_attach"}),
            name=prefix + "infrastructure-links",
        ),
        path(
            base + "resources/<uuid:resource_id>/project-links/<uuid:target_project_id>/",
            view.as_view({"delete": "links_detach"}),
            name=prefix + "infrastructure-link",
        ),
    ]


urlpatterns = infrastructure_urls(InfrastructureViewSet)
