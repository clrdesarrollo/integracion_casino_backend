"""
Construcción del informe de colaciones.

Replica la lógica del informe PDF de la solución C# (ReportService.cs):
las colaciones contabilizadas = marcaciones "Ok" en turno + marcaciones
"SinTurno" atribuidas a un turno mediante una ventana de gracia
+ colaciones de ingreso manual (turnos sin marcación, p. ej. la once que se deja
preparada: la cocinera registra la cantidad en el terminal).
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Shift, Station


#: Empresa con la que figuran en el informe las colaciones de ingreso manual (sin persona).
MANUAL_COMPANY = '(ingreso manual)'


@dataclass
class ShiftRow:
    shift: Shift
    en_turno: int = 0
    asociadas: int = 0
    manuales: int = 0            # registro de ingreso manual (sin marcaciones)

    @property
    def total(self):
        return self.en_turno + self.asociadas + self.manuales


@dataclass
class ReportData:
    date_from: datetime
    date_to: datetime            # exclusivo (día siguiente al último día)
    station: Station | None
    grace_minutes: int
    shift_name: str = ''      # '' = todos los turnos

    servidas: int = 0
    personas_unicas: int = 0
    duplicados: int = 0
    no_autorizados: int = 0
    sin_asociar: int = 0
    visitas: int = 0             # colaciones servidas a visitas (tarjeta RFID)
    manuales: int = 0            # colaciones de ingreso manual (incluidas en `servidas`)

    # Nombres de turno (desayuno, almuerzo, …) presentes en las colaciones servidas,
    # ordenados por hora del día. Son las columnas de la composición de `por_empresa`
    # y `por_persona`: cada colación servida se atribuye a exactamente uno de ellos.
    meal_types: list = field(default_factory=list)   # [str]

    por_dia: list = field(default_factory=list)      # [(date, total)]
    # En `por_empresa`/`por_persona`, `comp` es la lista de conteos por turno alineada
    # a `meal_types` (mismo orden); la suma de `comp` es el total de la fila.
    por_empresa: list = field(default_factory=list)  # [(empresa, total, comp)]
    por_persona: list = field(default_factory=list)  # [(nombre, empresa, total, comp)]
    por_turno: list = field(default_factory=list)    # [ShiftRow]

    @property
    def display_to(self):
        """Último día incluido (date_to es exclusivo)."""
        return timezone.localtime(self.date_to).date() if timezone.is_aware(self.date_to) \
            else self.date_to.date()


def _attribute_shift(event, shifts, grace_minutes):
    """
    Turno al que se atribuye una marcación fuera de turno (igual que el C#):
    el turno que empieza dentro de la ventana de gracia posterior a la marca;
    en su defecto, el último turno terminado antes de la marca.
    """
    et = event.event_time

    nexts = [s for s in shifts if s.started_at > et]
    nexts.sort(key=lambda s: s.started_at)
    if nexts:
        nxt = nexts[0]
        if (nxt.started_at - et).total_seconds() <= grace_minutes * 60:
            return nxt

    prevs = [s for s in shifts if s.ended_at is not None and s.ended_at <= et]
    prevs.sort(key=lambda s: s.ended_at, reverse=True)
    return prevs[0] if prevs else None


def _local_date(dt) -> date:
    return timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()


def build_report(date_from: datetime, date_to: datetime,
                 station: Station | None = None,
                 grace_minutes: int | None = None,
                 shift_name: str = '') -> ReportData:
    if grace_minutes is None:
        grace_minutes = settings.REPORT_GRACE_MINUTES

    events_qs = AccessEvent.objects.filter(event_time__gte=date_from, event_time__lt=date_to)
    shifts_qs = Shift.objects.filter(started_at__gte=date_from, started_at__lt=date_to)
    if station is not None:
        events_qs = events_qs.filter(station=station)
        shifts_qs = shifts_qs.filter(station=station)

    shift_name = (shift_name or '').strip()
    if shift_name:
        shifts_qs = shifts_qs.filter(name__iexact=shift_name)

    events = list(events_qs)
    all_shifts = list(shifts_qs)
    # Los registros de ingreso manual no son turnos con marcaciones: no participan en la
    # atribución de marcaciones fuera de turno; su cantidad se suma aparte.
    shifts = [s for s in all_shifts if s.manual_count is None]
    manual_shifts = [s for s in all_shifts if s.manual_count]

    # Filtrado por turno: solo las marcaciones de esos turnos. Las de fuera de turno se
    # conservan por ahora porque todavía pueden atribuirse a uno de ellos por la ventana
    # de gracia; las que no se atribuyan quedan descartadas (son de otro turno).
    selected_ids = {s.id for s in shifts} if shift_name else None
    if selected_ids is not None:
        events = [e for e in events
                  if e.shift_id in selected_ids or e.status == AccessEvent.Status.SIN_TURNO]

    data = ReportData(date_from=date_from, date_to=date_to,
                      station=station, grace_minutes=grace_minutes,
                      shift_name=shift_name)

    ok_events = [e for e in events if e.status == AccessEvent.Status.OK]

    # Marcaciones fuera de turno atribuidas (o no) a un turno.
    asociadas = []          # (event, shift)
    sin_asociar = 0
    for e in events:
        if e.status != AccessEvent.Status.SIN_TURNO:
            continue
        attributed = _attribute_shift(e, shifts, grace_minutes)
        if attributed is not None:
            asociadas.append((e, attributed))
        elif selected_ids is None:
            sin_asociar += 1

    served = ok_events + [e for (e, _s) in asociadas]

    data.manuales = sum(s.manual_count for s in manual_shifts)
    data.servidas = len(served) + data.manuales
    data.duplicados = sum(1 for e in events if e.status == AccessEvent.Status.DUPLICADO)
    data.no_autorizados = sum(1 for e in events if e.status == AccessEvent.Status.NO_AUTORIZADO)
    data.sin_asociar = sin_asociar
    data.visitas = sum(1 for e in served if e.is_visitor)
    data.personas_unicas = len({e.employee_no for e in served})

    # ---- Por día ----
    por_dia = defaultdict(int)
    for e in served:
        por_dia[_local_date(e.event_time)] += 1
    for s in manual_shifts:
        por_dia[s.service_date or _local_date(s.started_at)] += s.manual_count
    data.por_dia = sorted(por_dia.items())

    # ---- Turno atribuido a cada colación servida ----
    # Descompone los totales por tipo de colación (turno). Cada colación servida es una
    # marcación "Ok" (turno = su propio turno) o una "SinTurno" asociada (turno atribuido).
    shift_by_id = {s.id: s for s in shifts}
    missing_ids = {e.shift_id for e in ok_events
                   if e.shift_id is not None and e.shift_id not in shift_by_id}
    if missing_ids:
        for s in Shift.objects.filter(id__in=missing_ids):
            shift_by_id[s.id] = s

    def _turno_name(shift):
        name = (shift.name or '').strip() if shift else ''
        return name or '(sin turno)'

    served_turno = []  # (event, turno_name)
    for e in ok_events:
        served_turno.append((e, _turno_name(shift_by_id.get(e.shift_id))))
    for (e, s) in asociadas:
        served_turno.append((e, _turno_name(s)))

    # Orden de las columnas: por hora del día del turno (desayuno antes que almuerzo…);
    # los turnos sin hora conocida y '(sin turno)' quedan al final.
    turno_sort = {}
    for s in list(shift_by_id.values()) + manual_shifts:
        name = _turno_name(s)
        local = timezone.localtime(s.started_at) if timezone.is_aware(s.started_at) else s.started_at
        minutes = local.hour * 60 + local.minute
        turno_sort[name] = min(minutes, turno_sort.get(name, minutes))

    meal_types = sorted({name for (_e, name) in served_turno}
                        | {_turno_name(s) for s in manual_shifts},
                        key=lambda n: (turno_sort.get(n, 24 * 60 + 1), n.upper()))
    data.meal_types = meal_types
    idx = {name: i for i, name in enumerate(meal_types)}

    # ---- Por empresa (con composición por turno) ----
    emp_total = defaultdict(int)
    emp_comp = defaultdict(lambda: [0] * len(meal_types))
    for (e, name) in served_turno:
        key = e.company.strip() if e.company and e.company.strip() else '(sin empresa)'
        emp_total[key] += 1
        emp_comp[key][idx[name]] += 1
    # las de ingreso manual no tienen persona ni empresa: van en su propia fila
    for s in manual_shifts:
        emp_total[MANUAL_COMPANY] += s.manual_count
        emp_comp[MANUAL_COMPANY][idx[_turno_name(s)]] += s.manual_count
    data.por_empresa = [(emp, emp_total[emp], emp_comp[emp])
                        for emp in sorted(emp_total, key=lambda k: emp_total[k], reverse=True)]

    # ---- Por persona (con composición por turno) ----
    per_total = defaultdict(int)
    per_comp = defaultdict(lambda: [0] * len(meal_types))
    persona_info = {}
    for (e, name) in served_turno:
        key = (e.employee_no, e.person_name, e.company)
        per_total[key] += 1
        per_comp[key][idx[name]] += 1
        persona_info[key] = (e.person_name or '(desconocido)', e.company)
    data.por_persona = [(persona_info[k][0], persona_info[k][1], per_total[k], per_comp[k])
                        for k in sorted(per_total, key=lambda k: per_total[k], reverse=True)]

    # ---- Por turno ----
    en_turno_por_shift = defaultdict(int)
    for e in ok_events:
        if e.shift_id is not None:
            en_turno_por_shift[e.shift_id] += 1
    asociadas_por_shift = defaultdict(int)
    for (_e, s) in asociadas:
        asociadas_por_shift[s.id] += 1

    rows = []
    for s in sorted(shifts + manual_shifts, key=lambda x: x.started_at):
        rows.append(ShiftRow(
            shift=s,
            en_turno=en_turno_por_shift.get(s.id, 0),
            asociadas=asociadas_por_shift.get(s.id, 0),
            manuales=s.manual_count or 0,
        ))
    data.por_turno = rows

    return data
