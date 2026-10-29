from plane.api.views.team import TeamAPIEndpoint
from plane.app.urls.team import team_urls

urlpatterns = team_urls(TeamAPIEndpoint, "api-")
