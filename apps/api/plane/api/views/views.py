from plane.api.views.management import APIKeyManagementMixin
from plane.app.views.view.managed import ProjectViewEndpoint, ProjectViewPreviewEndpoint, ProjectViewWorkItemsEndpoint


class ProjectViewAPIEndpoint(APIKeyManagementMixin, ProjectViewEndpoint):
    pass


class ProjectViewPreviewAPIEndpoint(APIKeyManagementMixin, ProjectViewPreviewEndpoint):
    pass


class ProjectViewWorkItemsAPIEndpoint(APIKeyManagementMixin, ProjectViewWorkItemsEndpoint):
    pass
