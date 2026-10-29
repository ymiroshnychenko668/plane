from django.urls import path

from plane.app.views.team import TeamViewSet


def team_urls(view, prefix=""):
    project = "workspaces/<str:slug>/projects/<uuid:project_id>/team/"
    return [
        path(project, view.as_view({"get": "roster"}), name=prefix + "team"),
        path(
            project + "contacts/<str:address>/",
            view.as_view({"get": "contact_get", "put": "contact_put", "delete": "contact_delete"}),
            name=prefix + "team-contact",
        ),
        path(project + "resolve/", view.as_view({"post": "resolve"}), name=prefix + "team-resolve"),
        path(project + "agents/", view.as_view({"post": "agent_create"}), name=prefix + "team-agent-create"),
        path(
            project + "agents/<uuid:registration_id>/",
            view.as_view({"patch": "agent_patch"}),
            name=prefix + "team-agent",
        ),
        path(
            project + "members/<uuid:user_id>/",
            view.as_view({"post": "member_write", "patch": "member_write", "delete": "member_write"}),
            name=prefix + "team-member",
        ),
        path(project + "import/", view.as_view({"post": "import_legacy"}), name=prefix + "team-import"),
    ]


urlpatterns = team_urls(TeamViewSet)
