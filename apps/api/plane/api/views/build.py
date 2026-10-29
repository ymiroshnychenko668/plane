from plane.api.views.management import APIKeyManagementMixin
from plane.app.views.build import BuildViewSet


class BuildAPIEndpoint(APIKeyManagementMixin, BuildViewSet):
    """Same build semantics, with workspace-scoped API-key authentication."""
