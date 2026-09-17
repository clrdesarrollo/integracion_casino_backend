"""
Construcción del informe de colaciones.

Replica la lógica del informe PDF de la solución C# (ReportService.cs):
las colaciones contabilizadas = marcaciones "Ok" en turno + marcaciones
"SinTurno" atribuidas a un turno mediante una ventana de gracia.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Shift, Station


@dataclass
class ShiftRow:
    shift: Shift
    en_turno: int = 0
    asociadas: int = 0

    @property
    def total(self):
        return self.en_turno + self.asociadas


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

    por_dia: list = field(default_factory=list)      # [(date, total)]
    por_empresa: list = field(default_factory=list)  # [(empresa, total)]
    por_persona: list = field(default_factory=list)  # [(nombre, empresa, total)]
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
    shifts = list(shifts_qs)

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

    data.servidas = len(served)
    data.duplicados = sum(1 for e in events if e.status == AccessEvent.Status.DUPLICADO)
    data.no_autorizados = sum(1 for e in events if e.status == AccessEvent.Status.NO_AUTORIZADO)
    data.sin_asociar = sin_asociar
    data.visitas = sum(1 for e in served if e.is_visitor)
    data.personas_unicas = len({e.employee_no for e in served})

    # ---- Por día ----
    por_dia = defaultdict(int)
    for e in served:
        por_dia[_local_date(e.event_time)] += 1
    data.por_dia = sorted(por_dia.items())

    # ---- Por empresa ----
    por_empresa = defaultdict(int)
    for e in served:
        key = e.company.strip() if e.company and e.company.strip() else '(sin empresa)'
        por_empresa[key] += 1
    data.por_empresa = sorted(por_empresa.items(), key=lambda kv: kv[1], reverse=True)

    # ---- Por persona ----
    por_persona = defaultdict(int)
    persona_info = {}
    for e in served:
        key = (e.employee_no, e.person_name, e.company)
        por_persona[key] += 1
        persona_info[key] = (e.person_name or '(desconocido)', e.company)
    data.por_persona = sorted(
        ((persona_info[k][0], persona_info[k][1], v) for k, v in por_persona.items()),
        key=lambda t: t[2], reverse=True,
    )

    # ---- Por turno ----
    en_turno_por_shift = defaultdict(int)
    for e in ok_events:
        if e.shift_id is not None:
            en_turno_por_shift[e.shift_id] += 1
    asociadas_por_shift = defaultdict(int)
    for (_e, s) in asociadas:
        asociadas_por_shift[s.id] += 1

    rows = []
    for s in sorted(shifts, key=lambda x: x.started_at):
        rows.append(ShiftRow(
            shift=s,
            en_turno=en_turno_por_shift.get(s.id, 0),
            asociadas=asociadas_por_shift.get(s.id, 0),
        ))
    data.por_turno = rows

    return data
