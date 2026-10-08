"""
Ingreso manual de colaciones desde el backoffice.

Cuando el kiosco no pudo registrar una colación que sí se sirvió (corte de energía,
terminal apagado), un usuario con el permiso «Ingreso manual de colaciones» la registra
aquí, sobre un turno ya cerrado, eligiendo a la persona de la ficha de HikCentral.

Reglas, pensadas para que un error no ensucie la información:

- Solo sobre turnos cerrados y con marcaciones (no sobre registros de ingreso manual de la
  cocinera ni sobre un turno en curso: el terminal sigue mandando en él y no sabe de lo que
  se registra aquí, así que podría duplicarse).
- La persona se toma de la ficha de la estación: no se escriben nombres a mano.
- Una colación por persona y servicio: si ya tiene una válida (del terminal o manual) en el
  mismo turno o en una reapertura del mismo, se bloquea.
- La hora debe caer dentro de la ventana del turno.
- Lo que el terminal habría rechazado (no autorizada, sin colación, empresa fuera del turno,
  solo almuerzo en otro turno) se avisa y exige confirmación explícita; queda en la bitácora.
- Motivo obligatorio. La marcación nace con `origin='backoffice'`, quién la registró y
  cuándo. No se edita ni se borra: se anula (status Anulado) con motivo y queda la fila.
"""
import re
import unicodedata
from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from backend.apps.core import audit
from backend.apps.core.models import AccessEvent, Person, Shift, ShiftSchedule, new_uid

#: Texto con el que el ingreso manual figura como «método» en las marcaciones.
MANUAL_METHOD = 'Manual (backoffice)'

#: Más de estos días de antigüedad se avisa (el informe del período pudo ya emitirse).
OLD_SHIFT_DAYS = 31


class ManualEntryError(Exception):
    """El registro no procede; el mensaje es para el usuario."""


def fold(text):
    """Minúsculas y sin tildes, para buscar «munoz» y encontrar «Muñoz»."""
    text = unicodedata.normalize('NFD', (text or '').lower())
    return ''.join(ch for ch in text if unicodedata.category(ch) != 'Mn')


def digits_of(text):
    """Solo dígitos (y una K final de RUT): «12.345.678-9» → «123456789»."""
    raw = ''.join(ch for ch in (text or '').upper() if ch.isdigit() or ch == 'K')
    return raw


def shift_block_reason(shift):
    """Por qué NO se puede ingresar a mano en este turno (None si se puede)."""
    if shift.is_manual_entry:
        return ('Es un registro de ingreso manual de la cocinera (una cantidad, sin personas): '
                'no admite marcaciones.')
    if shift.ended_at is None:
        return ('El turno sigue en curso: el terminal todavía registra en él y no sabe de lo que '
                'se ingresa aquí, así que podría duplicarse. Espera a que cierre.')
    return None


def schedule_of(shift):
    if not shift.schedule_uid:
        return None
    return ShiftSchedule.objects.filter(station=shift.station, uid=shift.schedule_uid).first()


def shift_window(shift, schedule=None):
    """
    (inicio, término) entre los que se acepta la hora de la colación: la apertura real
    del turno y, si el horario programado termina después del cierre registrado (p. ej.
    un turno interrumpido que el terminal cerró al volver a arrancar), hasta ese horario.
    """
    start, end = shift.started_at, shift.ended_at
    schedule = schedule or schedule_of(shift)
    if schedule is not None and shift.service_date:
        tz = timezone.get_current_timezone()
        base = timezone.make_aware(datetime.combine(shift.service_date, time.min), tz)
        scheduled_end = base + timedelta(minutes=schedule.end_min)
        if schedule.end_min <= schedule.start_min:    # cruza la medianoche
            scheduled_end += timedelta(days=1)
        if end is None or scheduled_end > end:
            end = scheduled_end
    return start, end


def service_shift_ids(shift):
    """
    Turnos que son el MISMO servicio que `shift`: la apertura original y sus reaperturas,
    y toda apertura del mismo turno programado en el mismo día de servicio. La regla de
    una colación por persona aplica sobre el conjunto.
    """
    cond = Q(pk=shift.pk)
    root = shift.reopened_from_uid or shift.uid
    if root:
        cond |= Q(uid=root) | Q(reopened_from_uid=root)
    if shift.schedule_uid and shift.service_date:
        cond |= Q(schedule_uid=shift.schedule_uid, service_date=shift.service_date)
    return set(Shift.objects.filter(station=shift.station).filter(cond).values_list('pk', flat=True))


def served_in_service(shift):
    """{nº empleado: marcación válida} de todo el servicio (terminal y manuales)."""
    rows = (AccessEvent.objects
            .filter(shift_id__in=service_shift_ids(shift), status=AccessEvent.Status.OK)
            .defer('photo').order_by('event_time'))
    served = {}
    for ev in rows:
        served.setdefault(ev.employee_no, ev)
    return served


def person_checks(shift, person, served, schedule=None):
    """
    Qué impide o desaconseja registrar a `person` en `shift`.
    Devuelve {'blocked': [motivos], 'warnings': [motivos]}. Bloqueado = no se registra.
    Aviso = el terminal lo habría rechazado; se puede registrar confirmándolo expresamente.
    """
    blocked, warnings = [], []
    if person.is_visitor:
        blocked.append('Es una tarjeta de visita: las colaciones de visita se registran con la '
                       'tarjeta en el terminal, no a mano.')
    previous = served.get(person.employee_no)
    if previous is not None:
        when = timezone.localtime(previous.event_time)
        how = 'ingreso manual' if previous.is_manual else 'marcación del terminal'
        same = 'este turno' if previous.shift_id == shift.pk else 'una apertura del mismo servicio'
        blocked.append(f'Ya tiene una colación válida en {same}, a las {when:%H:%M:%S} ({how}).')

    if not person.authorized:
        warnings.append('En la ficha figura como NO autorizada: el terminal la habría rechazado.')
    if person.meal_policy == Person.MealPolicy.NONE:
        warnings.append('Su colación asignada en HikCentral es «Sin colación».')
    schedule = schedule or schedule_of(shift)
    if schedule is not None:
        if person.meal_policy == Person.MealPolicy.LUNCH_ONLY and not schedule.is_lunch:
            warnings.append(f'Tiene colación «Solo almuerzo» y «{schedule.name}» no es el turno '
                            'de almuerzo.')
        if not schedule.all_companies:
            allowed = {c.strip().upper() for c in schedule.company_names}
            if (person.company or '').strip().upper() not in allowed:
                empresa = person.company.strip() or '(sin empresa)'
                warnings.append(f'La empresa {empresa} no está autorizada en el turno '
                                f'«{schedule.name}».')
    return {'blocked': blocked, 'warnings': warnings}


def search_persons(shift, query, limit=12):
    """
    Personas de la estación que calzan con lo escrito: por nombre (sin tildes ni
    mayúsculas, todas las palabras) o por número / RUT (con o sin puntos, guion y dígito
    verificador). Cada una viene con sus avisos y bloqueos respecto del turno.
    """
    query = ' '.join((query or '').split())
    if len(query) < 2:
        return []
    digits = digits_of(query)
    # «12.345.678-9», «12345678-K», «8458387»: una búsqueda por número, no por nombre
    by_number = bool(re.fullmatch(r'[\d.\s-]*\d[\d.\s-]*[kK]?', query))
    words = [fold(w) for w in query.split()]

    persons = list(shift.station.persons.exclude(user_type='visitor').order_by('name', 'employee_no'))
    found = []
    for p in persons:
        if by_number:
            number = p.employee_no.upper()
            # el nº de HikCentral suele ser el RUT sin dígito verificador
            if not (digits in number or (len(digits) >= 8 and digits[:-1] in number)):
                continue
        else:
            name = fold(p.name)
            if not all(w in name for w in words):
                continue
        found.append(p)
        if len(found) >= limit:
            break

    served = served_in_service(shift)
    schedule = schedule_of(shift)
    out = []
    for p in found:
        checks = person_checks(shift, p, served, schedule)
        out.append({
            'id': p.pk,
            'name': p.name or f'Nº {p.employee_no}',
            'employee_no': p.employee_no,
            'company': p.company or '',
            'meal_policy': p.meal_policy_text,
            'authorized': p.authorized,
            'photo_url': p.photo_url,
            'blocked': checks['blocked'],
            'warnings': checks['warnings'],
        })
    return out


def resolve_time(shift, clock, window=None):
    """
    Hora escrita (time) → datetime aware dentro de la ventana del turno. Un turno que
    cruza la medianoche acepta horas de la madrugada siguiente.
    """
    start, end = window or shift_window(shift)
    tz = timezone.get_current_timezone()
    local_start = timezone.localtime(start)
    candidate = timezone.make_aware(datetime.combine(local_start.date(), clock), tz)
    if candidate < start:
        candidate += timedelta(days=1)
    if candidate < start or candidate > end:
        raise ManualEntryError(
            f'La hora debe estar dentro del turno: entre {timezone.localtime(start):%H:%M:%S} '
            f'y {timezone.localtime(end):%H:%M:%S}.')
    return candidate


def register_manual_event(shift, person, clock, reason, user, *, override=False, request=None):
    """
    Registra la colación. Lanza ManualEntryError con el motivo si no procede. Todo ocurre
    dentro de una transacción con el turno bloqueado: dos registros simultáneos de la misma
    persona no pueden colarse ambos.
    """
    reason = ' '.join((reason or '').split())
    if len(reason) < 10:
        raise ManualEntryError('Indica el motivo (al menos 10 caracteres): es lo que quedará en el '
                               'informe y en la bitácora.')
    with transaction.atomic():
        shift = Shift.objects.select_for_update().select_related('station').get(pk=shift.pk)
        why_not = shift_block_reason(shift)
        if why_not:
            raise ManualEntryError(why_not)
        if person.station_id != shift.station_id:
            raise ManualEntryError('La persona no pertenece a la estación del turno.')

        window = shift_window(shift)
        when = resolve_time(shift, clock, window)

        schedule = schedule_of(shift)
        checks = person_checks(shift, person, served_in_service(shift), schedule)
        if checks['blocked']:
            raise ManualEntryError(' '.join(checks['blocked']))
        if checks['warnings'] and not override:
            raise ManualEntryError(
                'Hay avisos sobre esta persona: revísalos y, si corresponde registrar igual, '
                'marca «Registrar de todos modos».')

        event = AccessEvent.objects.create(
            station=shift.station,
            uid=new_uid(),
            remote_id=0,                     # no viene del terminal: no tiene id allá
            shift=shift,
            shift_remote_id=shift.remote_id,
            employee_no=person.employee_no,
            person_name=person.name,
            company=person.company,
            verify_method=MANUAL_METHOD,
            event_time=when,
            status=AccessEvent.Status.OK,
            origin=AccessEvent.Origin.BACKOFFICE,
            entered_by=user,
            entered_by_name=user.full_name,
            entry_reason=reason,
        )
        summary = (f'Colación ingresada a mano: {person.name or person.employee_no} '
                   f'({person.employee_no}{", " + person.company if person.company else ""}) en el '
                   f'turno {shift.name} del {shift.service_date or timezone.localtime(shift.started_at).date():%d-%m-%Y}, '
                   f'a las {timezone.localtime(when):%H:%M:%S}. Motivo: {reason}')
        if checks['warnings']:
            summary += ' · Registrada pese a los avisos: ' + ' '.join(checks['warnings'])
        audit.log(
            'colacion.ingreso_manual', summary,
            request=request, user=user, shift=shift, event=event,
            level=audit.WARNING if checks['warnings'] else audit.INFO,
            data={
                'persona': person.name, 'numero': person.employee_no, 'empresa': person.company,
                'turno': shift.name, 'turno_id': shift.pk, 'hora': when.isoformat(),
                'motivo': reason, 'avisos': checks['warnings'],
            },
        )
    return event


def annul_manual_event(event, reason, user, request=None):
    """Anula un ingreso manual: la fila queda, deja de contar, y se anota quién y por qué."""
    reason = ' '.join((reason or '').split())
    if len(reason) < 5:
        raise ManualEntryError('Indica el motivo de la anulación.')
    with transaction.atomic():
        event = AccessEvent.objects.select_for_update().select_related('station', 'shift').get(pk=event.pk)
        if not event.is_manual:
            raise ManualEntryError('Solo se pueden anular las colaciones ingresadas a mano; las '
                                   'marcaciones del terminal no se tocan.')
        if event.is_annulled:
            raise ManualEntryError('Esa colación ya estaba anulada.')
        event.status = AccessEvent.Status.ANULADO
        event.annulled_at = timezone.now()
        event.annulled_by = user
        event.annulled_by_name = user.full_name
        event.annul_reason = reason
        event.save(update_fields=['status', 'annulled_at', 'annulled_by', 'annulled_by_name',
                                  'annul_reason'])
        audit.log(
            'colacion.anulada',
            f'Colación ingresada a mano ANULADA: {event.person_name or event.employee_no} '
            f'({event.employee_no}) en el turno {event.shift.name if event.shift else "—"} '
            f'a las {timezone.localtime(event.event_time):%d-%m-%Y %H:%M:%S}. '
            f'La había registrado {event.entered_by_name or "—"}. Motivo: {reason}',
            request=request, user=user, shift=event.shift, event=event, level=audit.WARNING,
            data={'persona': event.person_name, 'numero': event.employee_no,
                  'registrada_por': event.entered_by_name, 'motivo_ingreso': event.entry_reason,
                  'motivo_anulacion': reason},
        )
    return event


def manual_events_of(shift):
    """Ingresos manuales del turno (válidos y anulados), en orden de hora."""
    return list(shift.events.filter(origin=AccessEvent.Origin.BACKOFFICE)
                .defer('photo').order_by('event_time', 'pk'))


def is_old(shift):
    return (timezone.now() - shift.started_at) > timedelta(days=OLD_SHIFT_DAYS)
