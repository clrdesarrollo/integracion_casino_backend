"""
Configuración compartida entre el terminal (app CasinoAccess) y el backoffice:
turnos programados con sus empresas autorizadas y el set de tarjetas RFID de visitas.

Ambos lados pueden editarla. Cada lado guarda la marca de tiempo (UTC) de su última
edición; en cada sincronización se compara y GANA LA MÁS RECIENTE:
- si la del terminal es más nueva → se reemplaza la copia del servidor;
- si la del servidor es más nueva → se devuelve al terminal para que la aplique.
"""
from datetime import datetime, timezone as dt_tz

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from backend.apps.core.models import (
    ALL_DAYS_MASK, ShiftSchedule, ShiftScheduleCompany, Station, VisitorCard, new_uid,
)


def parse_stamp(value):
    """ISO 8601 (la app envía 'yyyy-MM-ddTHH:mm:ss.fffZ') → datetime aware UTC, o None."""
    if not value:
        return None
    dt = parse_datetime(str(value))
    if dt is None:
        try:
            dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        except ValueError:
            return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, dt_tz.utc)
    return dt


def format_stamp(dt):
    if dt is None:
        return ''
    dt = timezone.localtime(dt, dt_tz.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


def to_ms(dt):
    """Las marcas viajan con precisión de milisegundos: se compara a esa precisión."""
    return None if dt is None else dt.replace(microsecond=(dt.microsecond // 1000) * 1000)


def serialize_config(station: Station) -> dict:
    """Configuración de la estación con el formato que entiende la app (snake_case)."""
    schedules = []
    for s in station.schedules.prefetch_related('companies').order_by('start_min', 'id'):
        schedules.append({
            'uid': s.uid,
            'name': s.name,
            'start_min': s.start_min,
            'end_min': s.end_min,
            'enabled': s.enabled,
            'days_mask': s.days_mask,
            'all_companies': s.all_companies,
            'allow_visitors': s.allow_visitors,
            'is_lunch': s.is_lunch,
            'companies': [c.company for c in s.companies.all()],
        })
    cards = []
    for v in station.visitor_cards.order_by('created_at', 'card_no'):
        cards.append({
            'card_no': v.card_no,
            'label': v.label,
            'enabled': v.enabled,
            'created_at': timezone.localtime(v.created_at).strftime('%Y-%m-%dT%H:%M:%S'),
        })
    return {
        'updated_at': format_stamp(station.config_updated_at),
        'shift_overtime_minutes': station.shift_overtime_minutes,
        'schedules': schedules,
        'visitor_cards': cards,
    }


@transaction.atomic
def apply_config(station: Station, data: dict, updated_at) -> None:
    """Reemplaza la configuración del servidor por la recibida del terminal (más nueva)."""
    ShiftScheduleCompany.objects.filter(schedule__station=station).delete()
    station.schedules.all().delete()
    for i, s in enumerate(data.get('schedules') or []):
        sched = ShiftSchedule.objects.create(
            station=station,
            # el uid llega del terminal y se conserva: es lo que liga cada apertura
            # (Shift.schedule_uid) con su definición. Sin él, uno nuevo.
            uid=(s.get('uid') or '').strip() or new_uid(),
            name=(s.get('name') or '').strip() or 'Turno',
            start_min=max(0, min(1439, int(s.get('start_min') or 0))),
            end_min=max(0, min(1439, int(s.get('end_min') or 0))),
            enabled=bool(s.get('enabled', True)),
            # un terminal antiguo no manda days_mask: se asume todos los días
            days_mask=int(ALL_DAYS_MASK if s.get('days_mask') is None
                          else s['days_mask']) & ALL_DAYS_MASK,
            all_companies=bool(s.get('all_companies', True)),
            allow_visitors=bool(s.get('allow_visitors', True)),
            # un terminal antiguo no manda is_lunch: se decide por el nombre del turno
            is_lunch=(ShiftSchedule.looks_like_lunch(s.get('name'))
                      if s.get('is_lunch') is None else bool(s['is_lunch'])),
            order=i,
        )
        seen = set()
        for c in s.get('companies') or []:
            name = (c or '').strip()
            key = name.upper()
            if key in seen:
                continue
            seen.add(key)
            ShiftScheduleCompany.objects.create(schedule=sched, company=name)

    station.visitor_cards.all().delete()
    seen_cards = set()
    for v in data.get('visitor_cards') or []:
        no = VisitorCard.normalize(v.get('card_no'))
        if not no or no in seen_cards:
            continue
        seen_cards.add(no)
        created = parse_stamp(v.get('created_at')) or timezone.now()
        VisitorCard.objects.create(
            station=station, card_no=no,
            label=(v.get('label') or '').strip(),
            enabled=bool(v.get('enabled', True)),
            created_at=created,
        )

    fields = ['config_updated_at']
    overtime = data.get('shift_overtime_minutes')
    # Un terminal antiguo no manda la prórroga: se conserva la del servidor.
    if overtime is not None:
        station.shift_overtime_minutes = max(1, min(180, int(overtime)))
        fields.append('shift_overtime_minutes')

    station.config_updated_at = updated_at
    station.save(update_fields=fields)


def reconcile(station: Station, incoming: dict | None):
    """
    Decide quién gana. Devuelve la configuración a enviar al terminal (si la del servidor
    es más nueva) o None. Sin marca en el terminal y con datos en el servidor → gana el servidor.
    """
    if incoming is None:
        return None

    remote_stamp = to_ms(parse_stamp(incoming.get('updated_at')))
    local_stamp = to_ms(station.config_updated_at)

    if remote_stamp is None:
        # el terminal nunca editó: si el servidor tiene algo, se lo manda; si no, se
        # adopta lo del terminal como base (p. ej. sus turnos ya existentes)
        if local_stamp is not None:
            return serialize_config(station)
        if incoming.get('schedules') or incoming.get('visitor_cards'):
            apply_config(station, incoming, timezone.now())
        return None

    if local_stamp is None or remote_stamp > local_stamp:
        apply_config(station, incoming, remote_stamp)
        return None

    if local_stamp > remote_stamp:
        return serialize_config(station)

    return None  # iguales: nada que hacer
