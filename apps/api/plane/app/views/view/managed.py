"""Native-session project View evaluation; API-key wrappers reuse these handlers."""

from rest_framework.response import Response

from plane.app.views.base import BaseAPIView
from plane.services.views import ProjectViews, fail


class ProjectViewEndpoint(BaseAPIView):
    use_read_replica = False

    def service(self):
        return ProjectViews(self.request, self.kwargs["slug"], self.kwargs["project_id"])

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response

    def get(self, request, slug, project_id, pk=None):
        service = self.service()
        if pk is None:
            return Response(service.list(request.query_params))
        view = service.get(pk)
        return Response(service.serialize(view), headers={"ETag": f'"{view.revision}"'})

    def post(self, request, slug, project_id):
        service = self.service()
        view, created = service.create(request.data)
        return Response(service.serialize(view), status=201 if created else 200, headers={"ETag": f'"{view.revision}"'})

    def patch(self, request, slug, project_id, pk):
        service = self.service()
        view = service.update(pk, request.data)
        return Response(service.serialize(view), headers={"ETag": f'"{view.revision}"'})

    def delete(self, request, slug, project_id, pk):
        self.service().delete(pk)
        return Response(status=204)


class ProjectViewPreviewEndpoint(ProjectViewEndpoint):
    http_method_names = ["post", "options"]

    def post(self, request, slug, project_id):
        if not isinstance(request.data, dict) or set(request.data) - {
            "rich_filters",
            "filter_meta",
            "display_filters",
            "partition",
            "per_page",
            "cursor",
        }:
            fail(400, "validation_error", "Unknown preview fields.")
        definition = {
            key: request.data[key] for key in ("rich_filters", "filter_meta", "display_filters") if key in request.data
        }
        if "rich_filters" not in definition or "filter_meta" not in definition:
            fail(400, "validation_error", "rich_filters and filter_meta are required.")
        params = {key: request.data[key] for key in ("partition", "per_page", "cursor") if key in request.data}
        return Response(self.service().evaluate(definition, params))


class ProjectViewWorkItemsEndpoint(ProjectViewEndpoint):
    http_method_names = ["get", "head", "options"]

    def get(self, request, slug, project_id, pk):
        service = self.service()
        view = service.get(pk)
        if not view.filter_meta:
            fail(400, "unsupported_view", "This endpoint requires a versioned project View definition.")
        definition = {
            "rich_filters": view.rich_filters,
            "filter_meta": view.filter_meta,
            "display_filters": view.display_filters,
        }
        return Response(service.evaluate(definition, request.query_params, view=view))
