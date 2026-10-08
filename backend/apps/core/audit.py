"""
Bitácora del sistema.

Una línea por cada cosa que pasa y que después alguien va a querer reconstruir: turnos
abiertos y cerrados, el terminal que se conecta, se cae o vuelve a sincronizar, colaciones
ingresadas a mano, cambios de configuración, usuarios, visitas y sesiones.

`log()` nunca lanza: la bitácora no puede romper la operación que registra. La escritura va
en un savepoint propio para que, si fallara dentro de una transacción (ingesta del terminal),
no deje la transacción abortada.

Las acciones se nombran `<categoría>.<qué pasó>`; la categoría es el primer tramo y es lo
que se filtra en la pantalla.
"""
import logging

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

#: Categorías (primer tramo de la acción) con su etiqueta para la pantalla.
CATEGORIES = (
    ('terminal', 'Terminal'),
    ('turno', 'Turnos'),
    ('colacion', 'Colaciones'),
    ('visita', 'Visitas'),
    ('config', 'Configuración'),
    ('usuario', 'Usuarios y roles'),
    ('sesion', 'Sesiones'),
    ('correo', 'Correo'),
)
CATEGORY_LABELS = dict(CATEGORIES)

INFO, WARNING, ERROR = 'info', 'warning', 'error'


def client_ip(request):
    """IP de quien hace la petición (la primera de X-Forwarded-For si hay proxy)."""
    if request is None:
        return ''
    forwarded = request.META.get('HTTP_X_FORWARDED_FOR', '')
    if forwarded:
        return forwarded.split(',')[0].strip()[:45]
    return (request.META.get('REMOTE_ADDR') or '')[:45]


def log(action, summary, *, request=None, user=None, station=None, shift=None, event=None,
        data=None, level=INFO, at=None, ip=''):
    """
    Anota una línea en la bitácora. `at` es el instante del hecho (por defecto ahora: para
    un turno que el terminal informa después, se pasa su hora real y la de recepción va en
    `data`). `user` se toma de `request` si no se indica; el terminal y el sistema no
    llevan usuario.
    """
    from backend.apps.core.models import AuditLog

    try:
        if user is None and request is not None:
            candidate = getattr(request, 'user', None)
            if candidate is not None and getattr(candidate, 'is_authenticated', False):
                user = candidate
        if not ip:
            ip = client_ip(request)
        if station is None:
            if shift is not None:
                station = shift.station
            elif event is not None:
                station = event.station
        category = action.split('.', 1)[0]
        with transaction.atomic():
            return AuditLog.objects.create(
                at=at or timezone.now(),
                category=category,
                action=action[:60],
                level=level,
                summary=summary[:400],
                station=station,
                station_name=(station.name if station is not None else '')[:120],
                user=user if (user is not None and getattr(user, 'pk', None)) else None,
                user_name=(user.full_name if user is not None else '')[:200],
                ip=ip or '',
                shift=shift,
                event=event,
                data=data or {},
            )
    except Exception:   # noqa: BLE001 - la bitácora jamás interrumpe lo que registra
        logger.exception('No se pudo escribir en la bitácora: %s · %s', action, summary)
        return None


def duration_text(delta):
    """«3 min», «1 h 20 min», «2 d 4 h» para los resúmenes."""
    seconds = int(max(0, delta.total_seconds()))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f'{days} d {hours} h'
    if hours:
        return f'{hours} h {minutes} min'
    if minutes:
        return f'{minutes} min'
    return f'{seconds} s'


def local_text(dt, fmt='%d-%m-%Y %H:%M:%S'):
    """Hora local legible para los resúmenes ('' si no hay)."""
    if dt is None:
        return ''
    if timezone.is_aware(dt):
        dt = timezone.localtime(dt)
    return dt.strftime(fmt)
