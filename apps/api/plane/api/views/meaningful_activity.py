from plane.api.views.management import APIKeyManagementMixin
from plane.app.views.meaningful_activity import MeaningfulActivityEndpoint


class MeaningfulActivityAPIEndpoint(APIKeyManagementMixin, MeaningfulActivityEndpoint):
    pass
