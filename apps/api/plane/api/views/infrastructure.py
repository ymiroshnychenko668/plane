from plane.api.views.management import APIKeyManagementMixin
from plane.app.views.infrastructure import InfrastructureViewSet


class InfrastructureAPIEndpoint(APIKeyManagementMixin, InfrastructureViewSet):
    pass
