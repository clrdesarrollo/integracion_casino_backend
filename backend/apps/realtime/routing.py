from django.urls import re_path

from backend.apps.realtime import consumers

websocket_urlpatterns = [
    re_path(r'^ws/monitor/$', consumers.MonitorConsumer.as_asgi()),
]
