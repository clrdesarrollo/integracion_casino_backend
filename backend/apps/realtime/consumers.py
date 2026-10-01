import json
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Person, Shift
from backend.apps.realtime.broadcast import (
    MONITOR_GROUP, active_shifts, serialize_event, station_group,
)


class MonitorConsumer(AsyncWebsocketConsumer):
    """
    Monitor de colaciones en vivo. Requiere sesión iniciada (AuthMiddlewareStack) Y el
    permiso de ver colaciones emitidas: por aquí viajan las mismas marcaciones que la
    página, así que exigir solo sesión permitiría esquivar la restricción de la vista.
    Al conectar envía un snapshot con las marcaciones recientes y los contadores del
    día; luego recibe cada marcación nueva difundida por la ingesta.
    """

    RECENT_LIMIT = 40
    #: Cada cuánto se revalida el permiso de una conexión ya abierta.
    REVALIDATE_SECONDS = 30

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._checked_at = 0.0

    async def connect(self):
        user = self.scope.get('user')
        if user is None or not user.is_authenticated or not user.is_active:
            await self.close(code=4401)   # sin sesión válida
            return
        # el rol es una relación: se lee en un hilo de base de datos, no en el bucle async
        if not await self._puede_ver_monitor(user.pk):
            await self.close(code=4403)   # autenticado, pero su rol no ve el monitor
            return

        self._checked_at = time.monotonic()   # recién validado: no repetir en el primer evento
        await self.channel_layer.group_add(MONITOR_GROUP, self.channel_name)
        await self.accept()

        snapshot = await self._snapshot()
        await self.send(text_data=json.dumps({'type': 'snapshot', **snapshot}))

    async def disconnect(self, code):
        await self.channel_layer.group_discard(MONITOR_GROUP, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        # El cliente solo necesita mantener viva la conexión (ping opcional).
        if text_data == 'ping':
            await self.send(text_data='pong')

    async def monitor_event(self, message):
        """Handler del group_send type='monitor.event'."""
        if not await self._sigue_autorizado():
            await self.close(code=4403)
            return
        await self.send(text_data=json.dumps({'type': 'event', 'event': message['event']}))

    async def _sigue_autorizado(self):
        """
        Revalida el permiso durante la conexión. Comprobarlo solo al conectar dejaría a
        quien pierde el rol (o cuya cuenta se desactiva) recibiendo marcaciones mientras
        no cierre la pestaña. Se relee como mucho cada REVALIDATE_SECONDS para no
        consultar la base en cada marcación.
        """
        ahora = time.monotonic()
        if ahora - self._checked_at < self.REVALIDATE_SECONDS:
            return True
        self._checked_at = ahora
        return await self._puede_ver_monitor(self.scope['user'].pk)

    @database_sync_to_async
    def _puede_ver_monitor(self, user_id):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.select_related('role').filter(pk=user_id).first()
        return bool(user and user.is_active and user.has_cap('monitor'))

    # ---- Acceso a datos ----
    @database_sync_to_async
    def _snapshot(self):
        # Igual que el kiosco: la lista corresponde al TURNO ABIERTO. Sin turno no se
        # muestra nada (el kiosco tampoco registra nada mientras no haya turno iniciado).
        open_ids = list(Shift.objects.filter(ended_at__isnull=True).values_list('id', flat=True))
        if open_ids:
            recent_qs = (
                AccessEvent.objects.select_related('station').defer('photo')
                .filter(shift_id__in=open_ids)
                .order_by('-event_time')[:self.RECENT_LIMIT]
            )
            recent = [
                serialize_event(e, e.station.name if e.station_id else '')
                for e in reversed(Person.attach_photos_to_events(recent_qs))
            ]
        else:
            recent = []

        today = timezone.localdate()
        start = timezone.make_aware(
            timezone.datetime(today.year, today.month, today.day),
            timezone.get_current_timezone(),
        )
        today_qs = AccessEvent.objects.filter(event_time__gte=start)
        counts = {
            'served': today_qs.filter(status=AccessEvent.Status.OK).count(),
            'duplicados': today_qs.filter(status=AccessEvent.Status.DUPLICADO).count(),
            'sin_turno': today_qs.filter(status=AccessEvent.Status.SIN_TURNO).count(),
            'no_autorizados': today_qs.filter(status=AccessEvent.Status.NO_AUTORIZADO).count(),
            'visitas': today_qs.filter(status=AccessEvent.Status.OK, is_visitor=True).count(),
        }
        return {'events': recent, 'counts': counts, 'shifts': active_shifts()}


class StationConsumer(AsyncWebsocketConsumer):
    """
    Canal de órdenes hacia el terminal (kiosco). El terminal se conecta con la misma
    cabecera `Authorization: Api-Key <clave>` de la sincronización HTTP y queda escuchando;
    el backoffice le manda órdenes cortas («sync_persons», «sync») para que actúe al
    instante. El canal es solo un aviso: los datos siguen viajando por `/api/sync/`, y si
    el WebSocket está caído el terminal recoge lo pendiente en su sincronización periódica.
    """

    async def connect(self):
        self.group = None
        station_id = await self._station_from_key()
        if station_id is None:
            await self.close(code=4401)
            return
        self.group = station_group(station_id)
        await self.channel_layer.group_add(self.group, self.channel_name)
        await self.accept()
        # lo que se pidió mientras el terminal no estaba conectado
        if await self._refresh_pending(station_id):
            await self.send(text_data=json.dumps({'command': 'sync_persons'}))

    async def disconnect(self, code):
        if self.group:
            await self.channel_layer.group_discard(self.group, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if text_data == 'ping':
            await self.send(text_data='pong')

    async def station_command(self, message):
        """Handler del group_send type='station.command'."""
        await self.send(text_data=json.dumps({'command': message['command']}))

    def _api_key(self):
        for name, value in self.scope.get('headers', []):
            if name == b'authorization':
                kind, _, key = value.decode('latin-1').partition(' ')
                return key.strip() if kind.lower() == 'api-key' else ''
        return ''

    @database_sync_to_async
    def _station_from_key(self):
        from backend.apps.core.models import StationAPIKey

        key = self._api_key()
        if not key:
            return None
        try:
            # get_from_key ya descarta las claves revocadas
            api_key = StationAPIKey.objects.get_from_key(key)
        except StationAPIKey.DoesNotExist:
            return None
        if api_key.has_expired:
            return None
        station = api_key.station
        return station.pk if station and station.is_active else None

    @database_sync_to_async
    def _refresh_pending(self, station_id):
        from backend.apps.core.models import Station

        return Station.objects.filter(
            pk=station_id, persons_refresh_requested_at__isnull=False).exists()
