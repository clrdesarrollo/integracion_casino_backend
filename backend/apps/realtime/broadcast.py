"""Difusión de marcaciones en tiempo real hacia los monitores conectados (WebSocket)."""
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.utils import timezone

MONITOR_GROUP = 'monitor'


def station_group(station_id) -> str:
    """Grupo del canal de órdenes hacia el terminal de una estación."""
    return f'station_{station_id}'


def send_station_command(station_id, command: str) -> None:
    """
    Envía una orden al terminal por su WebSocket (`/ws/station/`). Nunca lanza: si el
    terminal no está conectado o el canal (Redis) no responde, la orden simplemente no
    llega por aquí y el terminal la recoge en su próxima sincronización HTTP.
    """
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        async_to_sync(layer.group_send)(station_group(station_id), {
            'type': 'station.command', 'command': command,
        })
    except Exception:
        pass


def active_shifts() -> list:
    """
    Turno abierto de cada estación (el que no tiene término). El kiosco no puede operar
    sin turno iniciado, así que una estación sin turno abierto está detenida.
    Lo usan la página del monitor, su endpoint de sondeo y el snapshot del WebSocket.
    """
    from backend.apps.core.models import Shift, Station  # import diferido: evita ciclos al cargar apps

    abiertos = {
        s.station_id: s
        for s in Shift.objects.filter(ended_at__isnull=True).order_by('started_at')
    }
    out = []
    for st in Station.objects.all():
        sh = abiertos.get(st.pk)
        out.append({
            'station_id': st.pk,
            'station': st.name,
            'active': sh is not None,
            'shift_name': sh.name if sh else '',
            'started_text': timezone.localtime(sh.started_at).strftime('%H:%M') if sh else '',
        })
    return out


def serialize_event(ev, station_name: str = '') -> dict:
    """
    Representación liviana de una marcación para las tarjetas del monitor. La foto no viaja:
    va la URL de la foto de la ficha, si antes se llamó a Person.attach_photos_to_events.
    """
    lt = timezone.localtime(ev.event_time)
    return {
        'photo_url': getattr(ev, 'person_photo_url', ''),
        'id': ev.id,
        'remote_id': ev.remote_id,
        'station_id': ev.station_id,
        'station': station_name,
        'employee_no': ev.employee_no,
        'person_name': ev.person_name or f'N° {ev.employee_no}',
        'company': ev.company or '',
        'verify_method': ev.verify_method or '',
        'status': ev.status,
        'is_visitor': bool(ev.is_visitor),
        'detail': ev.detail or '',
        'time_text': lt.strftime('%H:%M:%S'),
        'date_text': lt.strftime('%d-%m-%Y'),
        'short_when': lt.strftime('%d-%m %H:%M'),   # columna Fecha del dashboard
    }


def broadcast_events(events, station_name: str = '') -> None:
    """
    Envía las marcaciones al grupo del monitor. Nunca lanza: si el canal (Redis)
    no está disponible, la ingesta no debe fallar por ello.
    """
    layer = get_channel_layer()
    if layer is None:
        return
    try:
        from backend.apps.core.models import Person  # import diferido: evita ciclos al cargar apps
        Person.attach_photos_to_events(events)
    except Exception:
        pass   # sin foto la tarjeta muestra las iniciales
    for ev in events:
        try:
            async_to_sync(layer.group_send)(MONITOR_GROUP, {
                'type': 'monitor.event',
                'event': serialize_event(ev, station_name),
            })
        except Exception:
            pass
