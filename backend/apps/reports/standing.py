"""
Colaciones programadas («entrega en mano»).

Hay personas que reciben su colación sin pasar por el kiosco: cubren un turno largo lejos
del casino y la gente del casino se la lleva en mano. Para no cargar al kiosco con más
funciones, el backoffice la registra solo: una regla (`StandingMeal`) dice a quién, en qué
turno programado y qué días; el sistema la aplica cada día de servicio y deja constancia en
`StandingMealRun` (una fila por regla y día, que es lo que impide registrarla dos veces).

Cuándo se registra, por día de servicio:

- Si es feriado (`Holiday`) o cae en una excepción de la regla (vacaciones, licencia), se
  omite y queda anotado.
- Si ese día la persona sí tiene colación válida en el servicio (marcó en el terminal, o se
  le ingresó a mano), se omite: nunca se cuenta dos veces.
- Si el terminal informó el CIERRE del turno, se registra sobre esa apertura, con la hora
  de inicio del turno (cuando la recibe). Es el camino normal: al cierre ya se sabe si marcó.
- Si el turno sigue abierto pasada la tolerancia (término programado + prórroga de la
  estación + minutos de gracia de la regla), se registra igual sobre el turno abierto y se
  anota que fue sin cierre. Como el terminal no la conoce, si después llega una marcación
  suya para ese servicio, la programada se anula sola (`on_terminal_events`).
- Si el turno no se abrió, queda «sin turno» con aviso en la bitácora; si el turno aparece
  después (el terminal lo tenía guardado), se aplica en ese momento.

Dos disparadores llaman a lo mismo: la ingesta del terminal al recibir un turno nuevo o
cerrado (`on_shifts_reported`, tras confirmar la transacción) y el servicio programador en
cada ciclo (`process`), que además revisa los días pendientes de las últimas dos semanas.
Al guardar la regla se revisan todos sus días pendientes desde la vigencia (`catch_up`), sin
ese límite: así se cargan las colaciones ya entregadas de días anteriores.
La marcación nace con origen `programada`, sin usuario («Sistema»), ligada a la regla; se
anula como un ingreso manual y nunca se borra.
"""
import logging
from datetime import datetime, time, timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

from backend.apps.core import audit
from backend.apps.core.models import (
    AccessEvent, Holiday, Shift, StandingMeal, StandingMealRun, new_uid,
)
from backend.apps.reports.manual import served_in_service, service_shift_ids

logger = logging.getLogger(__name__)

#: Texto con el que la colación programada figura como «método» en las marcaciones.
SCHEDULED_METHOD = 'Programada (entrega en mano)'
#: Quién registra: nadie; el sistema.
SYSTEM_NAME = 'Sistema'
#: Días hacia atrás que revisa cada ciclo (una regla nueva, un turno que el terminal informa tarde).
LOOKBACK_DAYS = 14

Status = StandingMealRun.Status


# ---------------------------------------------------------------------
#  Horarios
# ---------------------------------------------------------------------
def scheduled_bounds(schedule, day):
    """(inicio, término) programados del turno para el día de servicio `day`, aware."""
    tz = timezone.get_current_timezone()
    base = timezone.make_aware(datetime.combine(day, time.min), tz)
    start = base + timedelta(minutes=schedule.start_min)
    end = base + timedelta(minutes=schedule.end_min)
    if schedule.end_min <= schedule.start_min:      # cruza la medianoche
        end += timedelta(days=1)
    return start, end


def deadline_for(meal, schedule, day):
    """
    Instante tras el cual, sin cierre informado por el terminal, la colación se registra
    igual: término programado del turno + prórroga de cierre de la estación + tolerancia.
    """
    _, end = scheduled_bounds(schedule, day)
    extra = (meal.station.shift_overtime_minutes or 0) + (meal.grace_minutes or 0)
    return end + timedelta(minutes=extra)


def shifts_of_day(meal, day):
    """Aperturas del turno de la regla en ese día de servicio (original y reaperturas), en orden."""
    return list(Shift.objects
                .filter(station=meal.station, schedule_uid=meal.schedule_uid, service_date=day,
                        manual_count__isnull=True)
                .order_by('started_at', 'pk'))


def due_days(meal, schedule, now, first=None):
    """
    Días de servicio que la regla debe revisar: desde `first` (por defecto, las últimas dos
    semanas) hasta hoy, en que rige la regla, el turno se sirve y su inicio programado ya
    pasó. Los días ya resueltos (registrada, omitida, anulada) no se vuelven a mirar.
    """
    today = timezone.localtime(now).date()
    if first is None:
        first = today - timedelta(days=LOOKBACK_DAYS)
    final = set(meal.runs.filter(service_date__gte=first)
                .exclude(status__in=(Status.PENDING, Status.NO_SHIFT))
                .values_list('service_date', flat=True))
    days = []
    for offset in range((today - first).days + 1):
        day = first + timedelta(days=offset)
        if day in final or not meal.applies_on(day) or not schedule.applies_on(day):
            continue
        if scheduled_bounds(schedule, day)[0] > now:
            continue
        days.append(day)
    return days


# ---------------------------------------------------------------------
#  Aplicación de la regla a un día
# ---------------------------------------------------------------------
def _run_for(meal, day):
    try:
        with transaction.atomic():
            run, _ = StandingMealRun.objects.get_or_create(meal=meal, service_date=day)
    except IntegrityError:
        # dos disparadores a la vez: el otro la creó, se usa esa
        run = StandingMealRun.objects.get(meal=meal, service_date=day)
    return run


def _set(run, status, detail, shift=None, event=None):
    changed = (run.status != status) or (run.detail != detail)
    run.status = status
    run.detail = detail[:300]
    if shift is not None:
        run.shift = shift
    if event is not None:
        run.event = event
    run.save(update_fields=['status', 'detail', 'shift', 'event', 'updated_at'])
    return changed


def _day_text(day):
    return f'{day:%d-%m-%Y}'


def evaluate(meal, day, now=None, schedule=None):
    """
    Aplica la regla al día de servicio `day` y devuelve su `StandingMealRun`. Idempotente:
    un día ya registrado, omitido o anulado no se toca.
    """
    now = now or timezone.now()
    run = _run_for(meal, day)
    if not run.is_open:
        return run
    schedule = schedule or meal.schedule
    label = f'{meal.person_name} ({meal.employee_no}), {meal.schedule_name} del {_day_text(day)}'

    if schedule is None:
        if _set(run, Status.SKIPPED, 'El turno programado ya no existe en la configuración de la estación.'):
            audit.log('colacion.programada_omitida',
                      f'Colación programada de {label} omitida: el turno programado ya no existe.',
                      station=meal.station, level=audit.WARNING, data={'regla_id': meal.pk})
        return run

    holiday = Holiday.on(day)
    if holiday is not None:
        if _set(run, Status.SKIPPED, f'Feriado: {holiday.name}.'):
            audit.log('colacion.programada_omitida',
                      f'Colación programada de {label} omitida: feriado ({holiday.name}).',
                      station=meal.station, data={'regla_id': meal.pk, 'feriado': holiday.name})
        return run

    exc = meal.exception_on(day)
    if exc is not None:
        if _set(run, Status.SKIPPED, f'Excepción: {exc.reason} ({exc.range_text}).'):
            audit.log('colacion.programada_omitida',
                      f'Colación programada de {label} omitida: {exc.reason} ({exc.range_text}).',
                      station=meal.station, data={'regla_id': meal.pk, 'excepcion': exc.reason})
        return run

    shifts = shifts_of_day(meal, day)
    if not shifts:
        if now >= deadline_for(meal, schedule, day):
            if _set(run, Status.NO_SHIFT,
                    'El turno no se abrió ese día, o el terminal aún no lo informa. Si aparece, '
                    'se registra entonces.'):
                audit.log('colacion.programada_sin_turno',
                          f'Colación programada de {label} SIN REGISTRAR: el turno no se abrió '
                          'ese día (o el terminal aún no lo informa). Si el turno llega después, '
                          'se registrará entonces.',
                          station=meal.station, level=audit.WARNING, data={'regla_id': meal.pk})
        else:
            _set(run, Status.PENDING, 'Esperando que el turno abra.')
        return run

    served = served_in_service(shifts[0])
    previous = served.get(meal.employee_no)
    if previous is not None:
        when = timezone.localtime(previous.event_time)
        how = ('ingreso manual' if previous.is_manual else
               'colación programada' if previous.is_scheduled else 'marcación del terminal')
        if _set(run, Status.SKIPPED, f'Ya tenía colación válida a las {when:%H:%M:%S} ({how}).',
                shift=previous.shift):
            audit.log('colacion.programada_omitida',
                      f'Colación programada de {label} omitida: ya tenía colación válida a las '
                      f'{when:%H:%M:%S} ({how}).',
                      station=meal.station, shift=previous.shift, data={'regla_id': meal.pk})
        return run

    closed = [s for s in shifts if s.ended_at is not None]
    if closed:
        return register(meal, run, closed[0], how='al cierre del turno')
    if now >= deadline_for(meal, schedule, day):
        return register(meal, run, shifts[0],
                        how='sin cierre informado por el terminal, pasada la tolerancia')
    _set(run, Status.PENDING, 'El turno sigue abierto: se registra al cierre.', shift=shifts[0])
    return run


def register(meal, run, shift, how):
    """
    Crea la marcación programada sobre `shift` con la hora de inicio del turno. La fila de
    ejecución se bloquea: si el otro disparador llegó primero, no se duplica.
    """
    with transaction.atomic():
        run = StandingMealRun.objects.select_for_update().get(pk=run.pk)
        if not run.is_open:
            return run
        shift = Shift.objects.select_related('station').get(pk=shift.pk)
        previous = served_in_service(shift).get(meal.employee_no)
        if previous is not None:
            when = timezone.localtime(previous.event_time)
            _set(run, Status.SKIPPED, f'Ya tenía colación válida a las {when:%H:%M:%S}.',
                 shift=previous.shift)
            return run

        person = meal.person
        event = AccessEvent.objects.create(
            station=shift.station,
            uid=new_uid(),
            remote_id=0,                     # no viene del terminal
            shift=shift,
            shift_remote_id=shift.remote_id,
            employee_no=meal.employee_no,
            person_name=person.name if person is not None else meal.person_name,
            company=person.company if person is not None else meal.company,
            verify_method=SCHEDULED_METHOD,
            event_time=shift.started_at,
            status=AccessEvent.Status.OK,
            origin=AccessEvent.Origin.SCHEDULED,
            entered_by=None,
            entered_by_name=SYSTEM_NAME,
            entry_reason=meal.note,
            standing_meal=meal,
        )
        detail = f'Registrada {how}, a las {timezone.localtime(event.event_time):%H:%M:%S}.'
        if person is None:
            detail += ' La persona ya no está en la ficha de la estación.'
        _set(run, Status.REGISTERED, detail, shift=shift, event=event)

        sin_cierre = shift.ended_at is None
        audit.log(
            'colacion.programada',
            f'Colación programada registrada: {event.person_name} ({event.employee_no}'
            f'{", " + event.company if event.company else ""}) en el turno {shift.name} del '
            f'{_day_text(run.service_date)}, a las {timezone.localtime(event.event_time):%H:%M:%S}, '
            f'{how}. Motivo: {meal.note}',
            shift=shift, event=event, level=audit.WARNING if sin_cierre else audit.INFO,
            data={'regla_id': meal.pk, 'persona': event.person_name, 'numero': event.employee_no,
                  'empresa': event.company, 'turno': shift.name, 'turno_id': shift.pk,
                  'sin_cierre': sin_cierre, 'motivo': meal.note},
        )
        if sin_cierre:
            # el turno sigue en curso: la colación se ve en el monitor en vivo como una más
            station_name = shift.station.name
            transaction.on_commit(lambda: _broadcast(event, station_name))
    return run


def _broadcast(event, station_name):
    try:
        from backend.apps.realtime.broadcast import broadcast_events
        broadcast_events([event], station_name)
    except Exception:   # noqa: BLE001 - el monitor nunca interrumpe el registro
        logger.exception('No se pudo difundir la colación programada al monitor')


# ---------------------------------------------------------------------
#  Disparadores
# ---------------------------------------------------------------------
def process(now=None):
    """
    Un ciclo del programador: revisa cada regla activa en los días que le toca y aplica lo
    que corresponda. Devuelve cuántas colaciones registró en este ciclo.
    """
    now = now or timezone.now()
    registered = 0
    meals = (StandingMeal.objects.filter(active=True)
             .select_related('station').prefetch_related('exceptions'))
    for meal in meals:
        schedule = meal.schedule
        if schedule is None or not schedule.enabled:
            continue
        for day in due_days(meal, schedule, now):
            try:
                before = StandingMealRun.objects.filter(meal=meal, service_date=day,
                                                        status=Status.REGISTERED).exists()
                run = evaluate(meal, day, now, schedule=schedule)
            except Exception:   # noqa: BLE001 - un día con error no detiene el resto
                logger.exception('Colación programada %s, día %s: error al aplicarla', meal.pk, day)
                continue
            if run.status == Status.REGISTERED and not before:
                registered += 1
    return registered


def catch_up(meal, now=None):
    """
    Aplica la regla a todos sus días pendientes desde «vigente desde», no solo a las dos
    semanas que mira el programador: con una vigencia en el pasado se cargan las colaciones
    ya entregadas de días anteriores, con las mismas reglas (feriados, excepciones, si marcó).
    Se llama al guardar la regla. Devuelve {estado: nº de días revisados ahora}.
    """
    now = now or timezone.now()
    schedule = meal.schedule
    if not meal.active or schedule is None or not schedule.enabled:
        return {}
    done = {}
    for day in due_days(meal, schedule, now, first=meal.valid_from):
        try:
            run = evaluate(meal, day, now, schedule=schedule)
        except Exception:   # noqa: BLE001 - un día con error no detiene el resto
            logger.exception('Colación programada %s, día %s: error al aplicarla', meal.pk, day)
            continue
        done[run.status] = done.get(run.status, 0) + 1
    return done


def on_shifts_reported(shift_ids, now=None):
    """
    El terminal informó estos turnos (nuevos o recién cerrados): se aplican las reglas de su
    turno programado para su día de servicio. Se llama tras confirmar la ingesta; nunca lanza.
    """
    now = now or timezone.now()
    try:
        shifts = Shift.objects.filter(pk__in=list(shift_ids), manual_count__isnull=True).select_related('station')
        for shift in shifts:
            if not shift.schedule_uid or shift.service_date is None:
                continue
            meals = (StandingMeal.objects
                     .filter(station=shift.station, schedule_uid=shift.schedule_uid, active=True)
                     .select_related('station').prefetch_related('exceptions'))
            for meal in meals:
                if not meal.applies_on(shift.service_date):
                    continue
                evaluate(meal, shift.service_date, now)
    except Exception:   # noqa: BLE001
        logger.exception('Colaciones programadas: error al aplicar las reglas de los turnos informados')


def on_terminal_events(event_ids, now=None):
    """
    Seguro contra el doble conteo: si la persona marcó en el terminal en un servicio donde
    ya tenía una colación programada registrada (se registró sin cierre, o el turno se
    reabrió), la programada se anula sola. Se llama tras confirmar la ingesta; nunca lanza.
    """
    now = now or timezone.now()
    try:
        events = (AccessEvent.objects
                  .filter(pk__in=list(event_ids), status=AccessEvent.Status.OK,
                          origin=AccessEvent.Origin.TERMINAL, is_visitor=False, shift__isnull=False)
                  .select_related('shift', 'station').defer('photo'))
        for terminal_event in events:
            scheduled = (AccessEvent.objects
                         .filter(station=terminal_event.station, employee_no=terminal_event.employee_no,
                                 origin=AccessEvent.Origin.SCHEDULED, status=AccessEvent.Status.OK,
                                 shift_id__in=service_shift_ids(terminal_event.shift))
                         .select_related('shift').defer('photo'))
            for event in scheduled:
                _auto_annul(event, terminal_event, now)
    except Exception:   # noqa: BLE001
        logger.exception('Colaciones programadas: error al conciliar con las marcaciones del terminal')


def _auto_annul(event, terminal_event, now):
    when = timezone.localtime(terminal_event.event_time)
    reason = (f'La persona marcó en el terminal a las {when:%H:%M:%S}: la programada se anula '
              'para no contar dos colaciones.')
    with transaction.atomic():
        event = AccessEvent.objects.select_for_update(of=('self',)).defer('photo').get(pk=event.pk)
        if event.is_annulled:
            return
        event.status = AccessEvent.Status.ANULADO
        event.annulled_at = now
        event.annulled_by = None
        event.annulled_by_name = SYSTEM_NAME
        event.annul_reason = reason
        event.save(update_fields=['status', 'annulled_at', 'annulled_by', 'annulled_by_name',
                                  'annul_reason'])
        StandingMealRun.objects.filter(event=event).update(
            status=Status.ANNULLED, detail=reason[:300], updated_at=now)
        audit.log(
            'colacion.programada_anulada',
            f'Colación programada de {event.person_name} ({event.employee_no}) ANULADA por el '
            f'sistema: marcó en el terminal a las {when:%H:%M:%S} en el mismo servicio '
            f'({terminal_event.shift.name}).',
            shift=event.shift, event=event,
            data={'marcacion_terminal_id': terminal_event.pk, 'motivo': reason},
        )
