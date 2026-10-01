from channels.auth import AuthMiddlewareStack
from channels.security.websocket import AllowedHostsOriginValidator
from django.urls import re_path

from backend.apps.realtime import consumers

websocket_urlpatterns = [
    # Navegadores del backoffice: sesión iniciada y origen del propio sitio.
    re_path(r'^ws/monitor/$',
            AllowedHostsOriginValidator(AuthMiddlewareStack(consumers.MonitorConsumer.as_asgi()))),
    # Terminales (kioscos): no son un navegador, no mandan Origin ni cookies; se
    # autentican con su API key dentro del propio consumer.
    re_path(r'^ws/station/$', consumers.StationConsumer.as_asgi()),
]
