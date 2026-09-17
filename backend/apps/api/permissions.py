from rest_framework_api_key.permissions import BaseHasAPIKey

from backend.apps.core.models import StationAPIKey


class HasStationAPIKey(BaseHasAPIKey):
    """
    Valida la cabecera `Authorization: Api-Key <clave>` contra una StationAPIKey.

    Si es válida, deja la estación en `request.station`.
    """

    model = StationAPIKey

    def has_permission(self, request, view):
        if not super().has_permission(request, view):
            return False

        key = self.get_key(request)
        if not key:
            return False

        try:
            api_key = self.model.objects.get_from_key(key)
        except self.model.DoesNotExist:
            return False

        station = api_key.station
        if not station or not station.is_active:
            return False

        request.station = station
        request.api_key = api_key
        return True
