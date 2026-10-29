from django.urls import path

from plane.api.views.build import BuildAPIEndpoint

project = "workspaces/<str:slug>/projects/<uuid:project_id>/"
urlpatterns = [
    path(project + "builds/", BuildAPIEndpoint.as_view({"get": "list"}), name="api-builds"),
    path(project + "builds-summary/", BuildAPIEndpoint.as_view({"get": "summary"}), name="api-builds-summary"),
    path(project + "builds/publish/", BuildAPIEndpoint.as_view({"post": "publish"}), name="api-builds-publish"),
    path(project + "builds/<uuid:pk>/", BuildAPIEndpoint.as_view({"get": "retrieve"}), name="api-builds-detail"),
]
