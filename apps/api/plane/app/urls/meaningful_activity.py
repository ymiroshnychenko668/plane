from django.urls import path
from plane.app.views.meaningful_activity import MeaningfulActivityEndpoint

BASE = "workspaces/<str:slug>/projects/<uuid:project_id>/"
ITEM = BASE + "work-items/<uuid:issue_id>/activity-assessments/"
urlpatterns = [
    path(
        BASE + "activity-assessments/pending/",
        MeaningfulActivityEndpoint.as_view(http_method_names=["get"]),
        {"action": "pending"},
        name="activity-pending",
    ),
    path(
        ITEM + "summary/",
        MeaningfulActivityEndpoint.as_view(http_method_names=["get"]),
        {"action": "summary"},
        name="activity-summary",
    ),
    path(
        ITEM + "candidates/",
        MeaningfulActivityEndpoint.as_view(http_method_names=["get"]),
        {"action": "candidates"},
        name="activity-candidates",
    ),
    path(
        ITEM + "assess/",
        MeaningfulActivityEndpoint.as_view(http_method_names=["post"]),
        {"action": "assess"},
        name="activity-assess",
    ),
    path(
        ITEM + "<uuid:assessment_id>/",
        MeaningfulActivityEndpoint.as_view(http_method_names=["get"]),
        {"action": "get"},
        name="activity-get",
    ),
    path(
        ITEM + "<uuid:assessment_id>/retract/",
        MeaningfulActivityEndpoint.as_view(http_method_names=["post"]),
        {"action": "retract"},
        name="activity-retract",
    ),
]
