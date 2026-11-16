from django.urls import path
from plane.api.views.meaningful_activity import MeaningfulActivityAPIEndpoint

BASE = "workspaces/<str:slug>/projects/<uuid:project_id>/"
ITEM = BASE + "work-items/<uuid:issue_id>/activity-assessments/"
urlpatterns = [
    path(
        BASE + "activity-assessments/pending/",
        MeaningfulActivityAPIEndpoint.as_view(http_method_names=["get"]),
        {"action": "pending"},
        name="api-activity-pending",
    ),
    path(
        ITEM + "summary/",
        MeaningfulActivityAPIEndpoint.as_view(http_method_names=["get"]),
        {"action": "summary"},
        name="api-activity-summary",
    ),
    path(
        ITEM + "candidates/",
        MeaningfulActivityAPIEndpoint.as_view(http_method_names=["get"]),
        {"action": "candidates"},
        name="api-activity-candidates",
    ),
    path(
        ITEM + "assess/",
        MeaningfulActivityAPIEndpoint.as_view(http_method_names=["post"]),
        {"action": "assess"},
        name="api-activity-assess",
    ),
    path(
        ITEM + "<uuid:assessment_id>/",
        MeaningfulActivityAPIEndpoint.as_view(http_method_names=["get"]),
        {"action": "get"},
        name="api-activity-get",
    ),
    path(
        ITEM + "<uuid:assessment_id>/retract/",
        MeaningfulActivityAPIEndpoint.as_view(http_method_names=["post"]),
        {"action": "retract"},
        name="api-activity-retract",
    ),
]
