from plane.api.views.management import APIKeyManagementMixin
from plane.app.views.team import TeamViewSet


class TeamAPIEndpoint(APIKeyManagementMixin, TeamViewSet):
    """Canonical Teams semantics with workspace-scoped API-key authentication."""
