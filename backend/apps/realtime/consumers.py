import json
import time

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Shift
from backend.apps.realtime.broadcast import MONITOR_GROUP, active_shifts, serialize_event


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
        if not user.can_see_tickets:
            await self.close(code=4403)   # autenticado, pero su rol no ve colaciones
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
        return await self._puede_ver_tickets(self.scope['user'].pk)

    @database_sync_to_async
    def _puede_ver_tickets(self, user_id):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.filter(pk=user_id).first()
        return bool(user and user.is_active and user.can_see_tickets)

    # ---- Acceso a datos ----
    @database_sync_to_async
    def _snapshot(self):
        # Igual que el kiosco: la lista corresponde al TURNO ABIERTO. Sin turno no se
        # muestra nada (el kiosco tampoco registra nada mientras no haya turno iniciado).
        open_ids = list(Shift.objects.filter(ended_at__isnull=True).values_list('id', flat=True))
        if open_ids:
            recent_qs = (
                AccessEvent.objects.select_related('station')
                .filter(shift_id__in=open_ids)
                .order_by('-event_time')[:self.RECENT_LIMIT]
            )
            recent = [
                serialize_event(e, e.station.name if e.station_id else '')
                for e in reversed(list(recent_qs))
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
