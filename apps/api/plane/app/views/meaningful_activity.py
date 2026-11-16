from rest_framework.response import Response

from plane.app.views.base import BaseAPIView
from plane.services.meaningful_activity import ProjectActivity, serialize_assessment
from plane.services.views import fail


class MeaningfulActivityEndpoint(BaseAPIView):
    use_read_replica = False

    def service(self):
        return ProjectActivity(self.request, self.kwargs["slug"], self.kwargs["project_id"])

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def get(self, request, slug, project_id, action, issue_id=None, assessment_id=None):
        service = self.service()
        if action in {"pending", "candidates"}:
            return Response(service.candidates(request.query_params, issue_id=issue_id))
        if action == "summary":
            return Response(service.summary(issue_id, request.query_params))
        value = service.get(issue_id, assessment_id, request.query_params)
        return Response(serialize_assessment(value), headers={"ETag": f'"{value.revision}"'})

    def post(self, request, slug, project_id, action, issue_id, assessment_id=None):
        if request.query_params:
            fail(400, "validation_error", "Assessment writes do not accept query parameters.")
        service = self.service()
        if action == "assess":
            value, created = service.assess(issue_id, request.data)
        elif action == "retract":
            value = service.retract(issue_id, assessment_id, request.data)
            created = False
        else:
            fail(405, "method_not_allowed", "Unsupported assessment operation.")
        return Response(
            serialize_assessment(value), status=201 if created else 200, headers={"ETag": f'"{value.revision}"'}
        )
