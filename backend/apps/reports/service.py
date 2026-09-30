"""
Construcción del informe de colaciones.

Las colaciones contabilizadas = marcaciones "Ok" en turno
+ colaciones de ingreso manual (turnos sin marcación, p. ej. la once que se deja
preparada: la cocinera registra la cantidad en el terminal).

Las marcaciones "SinTurno" no suman: el kiosco las rechaza y no se sirve colación.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from django.utils import timezone

from backend.apps.core.models import AccessEvent, Shift, Station, Visit, VisitorCard


#: Empresa con la que figuran en el informe las colaciones de ingreso manual (sin persona).
MANUAL_COMPANY = '(ingreso manual)'


@dataclass
class ShiftRow:
    shift: Shift
    en_turno: int = 0
    manuales: int = 0            # registro de ingreso manual (sin marcaciones)

    @property
    def total(self):
        return self.en_turno + self.manuales


@dataclass
class VisitRow:
    """
    Una visita registrada (a quién se entregó la tarjeta) con las colaciones que retiró en
    el período; o una tarjeta usada sin registro de visita, para que se note.
    """
    visit: Visit | None
    card_no: str
    card_label: str = ''
    station: Station | None = None
    colaciones: int = 0
    first_at: datetime | None = None   # entrega (o primera colación si no hay registro)

    @property
    def registrada(self):
        return self.visit is not None

    @property
    def visitor_name(self):
        return self.visit.visitor_name if self.visit else '(sin registro de visita)'

    @property
    def visitor_document(self):
        return self.visit.visitor_document if self.visit else ''

    @property
    def visitor_company(self):
        return self.visit.visitor_company if self.visit else ''

    @property
    def host_display(self):
        return self.visit.host_display if self.visit else ''

    @property
    def card_display(self):
        return self.card_label or f'Tarjeta {self.card_no}'

    @property
    def delivered_by_name(self):
        return self.visit.delivered_by_name if self.visit else ''

    @property
    def delivered_at(self):
        return self.visit.delivered_at if self.visit else None

    @property
    def returned_at(self):
        return self.visit.returned_at if self.visit else None


@dataclass
class ReportData:
    date_from: datetime
    date_to: datetime            # exclusivo (día siguiente al último día)
    station: Station | None
    shift_name: str = ''      # '' = todos los turnos

    servidas: int = 0
    personas_unicas: int = 0
    duplicados: int = 0
    no_autorizados: int = 0
    sin_turno: int = 0           # marcaciones fuera de turno (rechazadas, no suman)
    visitas: int = 0             # colaciones servidas a visitas (tarjeta RFID)
    visitas_sin_registro: int = 0  # de esas, con tarjeta entregada sin registro de visita
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
    # Marcaciones rechazadas por no estar autorizadas (empresa, colación asignada o
    # tarjeta de visita), en orden cronológico. `detail` trae el motivo.
    no_autorizados_detalle: list = field(default_factory=list)  # [AccessEvent]
    # Registro de visitas del período (a quién se entregó cada tarjeta, quién la entregó y
    # a quién venía a ver) con las colaciones que retiró cada una; incluye las tarjetas
    # usadas sin registro. Orden cronológico por entrega.
    visitas_detalle: list = field(default_factory=list)  # [VisitRow]

    @property
    def display_to(self):
        """Último día incluido (date_to es exclusivo)."""
        return timezone.localtime(self.date_to).date() if timezone.is_aware(self.date_to) \
            else self.date_to.date()


def _local_date(dt) -> date:
    return timezone.localtime(dt).date() if timezone.is_aware(dt) else dt.date()


def build_report(date_from: datetime, date_to: datetime,
                 station: Station | None = None,
                 shift_name: str = '') -> ReportData:
    events_qs = (AccessEvent.objects
                 .filter(event_time__gte=date_from, event_time__lt=date_to)
                 .select_related('station', 'shift'))
    shifts_qs = Shift.objects.filter(started_at__gte=date_from, started_at__lt=date_to)
    if station is not None:
        events_qs = events_qs.filter(station=station)
        shifts_qs = shifts_qs.filter(station=station)

    shift_name = (shift_name or '').strip()
    if shift_name:
        shifts_qs = shifts_qs.filter(name__iexact=shift_name)

    events = list(events_qs)
    all_shifts = list(shifts_qs)
    # Los registros de ingreso manual no son turnos con marcaciones: su cantidad se suma aparte.
    shifts = [s for s in all_shifts if s.manual_count is None]
    manual_shifts = [s for s in all_shifts if s.manual_count]

    # Filtrado por turno: solo las marcaciones de esos turnos (las de fuera de turno no
    # pertenecen a ninguno, así que quedan fuera).
    if shift_name:
        selected_ids = {s.id for s in shifts}
        events = [e for e in events if e.shift_id in selected_ids]

    data = ReportData(date_from=date_from, date_to=date_to,
                      station=station,
                      shift_name=shift_name)

    ok_events = [e for e in events if e.status == AccessEvent.Status.OK]
    served = ok_events

    data.manuales = sum(s.manual_count for s in manual_shifts)
    data.servidas = len(served) + data.manuales
    data.duplicados = sum(1 for e in events if e.status == AccessEvent.Status.DUPLICADO)
    data.no_autorizados_detalle = sorted(
        (e for e in events if e.status == AccessEvent.Status.NO_AUTORIZADO),
        key=lambda e: e.event_time)
    data.no_autorizados = len(data.no_autorizados_detalle)
    data.sin_turno = sum(1 for e in events if e.status == AccessEvent.Status.SIN_TURNO)
    data.visitas = sum(1 for e in served if e.is_visitor)
    data.personas_unicas = len({e.employee_no for e in served})

    # ---- Visitas: a quién se le había entregado la tarjeta en cada colación ----
    visitor_ok = [e for e in served if e.is_visitor]
    Visit.attach_to_events(visitor_ok)
    data.visitas_sin_registro = sum(1 for e in visitor_ok if e.visit is None)
    data.visitas_detalle = _visit_rows(visitor_ok, date_from, date_to, station,
                                       solo_con_colaciones=bool(shift_name))

    # ---- Por día ----
    por_dia = defaultdict(int)
    for e in served:
        por_dia[_local_date(e.event_time)] += 1
    for s in manual_shifts:
        por_dia[s.service_date or _local_date(s.started_at)] += s.manual_count
    data.por_dia = sorted(por_dia.items())

    # ---- Turno atribuido a cada colación servida ----
    # Descompone los totales por tipo de colación (turno): cada colación servida es una
    # marcación "Ok" y su turno es el de la propia marcación.
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
        visit = getattr(e, 'visit', None)
        if visit is not None:
            # dos visitas distintas con la misma tarjeta son dos personas distintas
            key = ('visita', visit.pk)
            label = f'{visit.visitor_name} · {visit.card_display}'
        else:
            key = (e.employee_no, e.person_name, e.company)
            label = e.person_name or '(desconocido)'
        per_total[key] += 1
        per_comp[key][idx[name]] += 1
        persona_info[key] = (label, e.company)
    data.por_persona = [(persona_info[k][0], persona_info[k][1], per_total[k], per_comp[k])
                        for k in sorted(per_total, key=lambda k: per_total[k], reverse=True)]

    # ---- Por turno ----
    en_turno_por_shift = defaultdict(int)
    for e in ok_events:
        if e.shift_id is not None:
            en_turno_por_shift[e.shift_id] += 1

    rows = []
    for s in sorted(shifts + manual_shifts, key=lambda x: x.started_at):
        rows.append(ShiftRow(
            shift=s,
            en_turno=en_turno_por_shift.get(s.id, 0),
            manuales=s.manual_count or 0,
        ))
    data.por_turno = rows

    return data


def _visit_rows(visitor_ok, date_from, date_to, station, solo_con_colaciones=False):
    """
    Filas de la sección «Visitas»: las visitas registradas cuya entrega cae en el período
    (aunque no hayan retirado colación), más las que retiraron colación en el período
    habiéndose entregado antes, más las tarjetas usadas sin registro de visita.
    Con filtro por turno solo interesan las que retiraron en ese turno.
    """
    visits_qs = (Visit.objects.filter(delivered_at__gte=date_from, delivered_at__lt=date_to)
                 .select_related('station').order_by('delivered_at'))
    if station is not None:
        visits_qs = visits_qs.filter(station=station)

    rows = {}
    for v in visits_qs:
        rows[('v', v.pk)] = VisitRow(visit=v, card_no=v.card_no, card_label=v.card_label,
                                     station=v.station, first_at=v.delivered_at)

    labels = None
    for e in visitor_ok:
        if e.visit is not None:
            key = ('v', e.visit.pk)
            if key not in rows:
                rows[key] = VisitRow(visit=e.visit, card_no=e.visit.card_no,
                                     card_label=e.visit.card_label, station=e.station,
                                     first_at=e.visit.delivered_at)
        else:
            key = ('c', e.station_id, e.card_no)
            if key not in rows:
                if labels is None:
                    labels = {(c.station_id, c.card_no): c.label for c in VisitorCard.objects.all()}
                rows[key] = VisitRow(visit=None, card_no=e.card_no,
                                     card_label=labels.get((e.station_id, e.card_no), ''),
                                     station=e.station, first_at=e.event_time)
            elif e.event_time < rows[key].first_at:
                rows[key].first_at = e.event_time
        rows[key].colaciones += 1

    out = list(rows.values())
    if solo_con_colaciones:
        out = [r for r in out if r.colaciones]
    out.sort(key=lambda r: (r.first_at, r.card_no))
    return out
