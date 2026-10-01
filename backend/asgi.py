import os

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'backend.settings')

from django.core.asgi import get_asgi_application

# Se inicializa Django (apps) ANTES de importar consumers que usan modelos.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter

import backend.apps.realtime.routing

application = ProtocolTypeRouter({
    'http': django_asgi_app,
    # cada ruta trae su propia autenticación (ver realtime/routing.py)
    'websocket': URLRouter(backend.apps.realtime.routing.websocket_urlpatterns),
})
