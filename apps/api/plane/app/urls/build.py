from django.urls import path

from plane.app.views.build import BuildViewSet

project = "workspaces/<str:slug>/projects/<uuid:project_id>/"
urlpatterns = [
    path(project + "builds/", BuildViewSet.as_view({"get": "list"}), name="builds"),
    path(project + "builds-summary/", BuildViewSet.as_view({"get": "summary"}), name="builds-summary"),
    path(project + "builds/<uuid:pk>/", BuildViewSet.as_view({"get": "retrieve"}), name="builds-detail"),
]
