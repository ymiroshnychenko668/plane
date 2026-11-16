from plane.api.views.infrastructure import InfrastructureAPIEndpoint
from plane.app.urls.infrastructure import infrastructure_urls

urlpatterns = infrastructure_urls(InfrastructureAPIEndpoint, "api-")
