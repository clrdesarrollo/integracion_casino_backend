from datetime import date, datetime, time, timedelta

from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Person, Shift, Station
from backend.apps.webapp.permissions import capability_required
from backend.apps.reports.excel import build_excel, build_shift_excel
from backend.apps.reports.pdf import build_pdf
from backend.apps.reports.service import build_report

MESES = ['', 'enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
         'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre']


def _month_range(today=None):
    """Primer día del mes actual hasta hoy (rango por defecto)."""
    today = today or timezone.localdate()
    return today.replace(day=1), today


def _parse_date(value, fallback):
    if not value:
        return fallback
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        return fallback


def _aware(d, end=False):
    """date -> datetime aware en la zona local (end=True usa fin del día)."""
    t = time.max if end else time.min
    naive = datetime.combine(d, t)
    return timezone.make_aware(naive, timezone.get_current_timezone())


def _resolve_params(request):
    default_from, default_to = _month_range()
    d_from = _parse_date(request.GET.get('from'), default_from)
    d_to = _parse_date(request.GET.get('to'), default_to)
    if d_to < d_from:
        d_from, d_to = d_to, d_from

    station = None
    station_id = request.GET.get('station')
    if station_id:
        station = Station.objects.filter(pk=station_id).first()

    shift_name = (request.GET.get('shift') or '').strip()

    # date_to exclusivo = día siguiente al último día incluido
    dt_from = _aware(d_from)
    dt_to = _aware(d_to + timedelta(days=1))
    return d_from, d_to, dt_from, dt_to, station, shift_name


def _last_day_of_month(d: date) -> date:
    first_next = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first_next - timedelta(days=1)


def _titulo(d_from: date, d_to: date, shift_name: str = '') -> str:
    if (d_from.day == 1 and d_from.month == d_to.month
            and d_from.year == d_to.year and d_to == _last_day_of_month(d_from)):
        base = f'Informe mensual — {MESES[d_from.month]} {d_from.year}'
    elif d_from == d_to:
        base = f'Informe diario — {d_from:%d-%m-%Y}'
    else:
        base = f'Informe {d_from:%d-%m-%Y} al {d_to:%d-%m-%Y}'
    return f'{base} · turno {shift_name}' if shift_name else base


def _shift_names(station=None):
    """Nombres de turno distintos que existen en los datos (para el desplegable)."""
    qs = Shift.objects.all()
    if station is not None:
        qs = qs.filter(station=station)
    return sorted({(n or '').strip() for n in qs.values_list('name', flat=True) if (n or '').strip()},
                  key=lambda s: s.upper())


@capability_required('reports')
def report_view(request):
    d_from, d_to, dt_from, dt_to, station, shift_name = _resolve_params(request)
    data = build_report(dt_from, dt_to, station=station, shift_name=shift_name)

    context = {
        'data': data,
        'titulo': _titulo(d_from, d_to, shift_name),
        'd_from': d_from,
        'd_to': d_to,
        'station': station,
        'stations': Station.objects.all(),
        'shift_name': shift_name,
        'shift_names': _shift_names(station),
    }
    return render(request, 'reports/report.html', context)


@capability_required('reports')
def report_pdf(request):
    d_from, d_to, dt_from, dt_to, station, shift_name = _resolve_params(request)
    data = build_report(dt_from, dt_to, station=station, shift_name=shift_name)
    pdf = build_pdf(data, _titulo(d_from, d_to, shift_name))

    resp = HttpResponse(pdf, content_type='application/pdf')
    fname = f'Informe_{d_from:%Y%m%d}_{d_to:%Y%m%d}.pdf'
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


@capability_required('reports')
def report_excel(request):
    d_from, d_to, dt_from, dt_to, station, shift_name = _resolve_params(request)
    data = build_report(dt_from, dt_to, station=station, shift_name=shift_name)
    xlsx = build_excel(data, _titulo(d_from, d_to, shift_name))

    resp = HttpResponse(
        xlsx,
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    fname = f'Informe_{d_from:%Y%m%d}_{d_to:%Y%m%d}.xlsx'
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


# =====================================================================
#  Detalle de colaciones: cada apertura, cómo se abrió/cerró y quién marcó
# =====================================================================
def _event_count(status):
    return Count('events', filter=Q(events__status=status))


@capability_required('shifts')
def shift_list(request):
    """
    Turnos realizados en el período (por defecto, los últimos 7 días): una fila por
    apertura con su hora de inicio y término al segundo, si fue automática o manual, y
    el resumen de marcaciones. Cada fila lleva al detalle con todas las marcaciones.
    """
    today = timezone.localdate()
    d_from = _parse_date(request.GET.get('from'), today - timedelta(days=6))
    d_to = _parse_date(request.GET.get('to'), today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from

    shifts = (Shift.objects
              .filter(started_at__gte=_aware(d_from), started_at__lt=_aware(d_to + timedelta(days=1)))
              .select_related('station')
              .annotate(n_ok=_event_count(AccessEvent.Status.OK),
                        n_dup=_event_count(AccessEvent.Status.DUPLICADO),
                        n_denied=_event_count(AccessEvent.Status.NO_AUTORIZADO))
              .order_by('-started_at'))

    station = None
    if request.GET.get('station'):
        station = Station.objects.filter(pk=request.GET['station']).first()
        if station is not None:
            shifts = shifts.filter(station=station)

    shift_name = (request.GET.get('shift') or '').strip()
    if shift_name:
        shifts = shifts.filter(name__iexact=shift_name)

    return render(request, 'reports/shift_list.html', {
        'shifts': shifts,
        'd_from': d_from,
        'd_to': d_to,
        'station': station,
        'stations': Station.objects.all(),
        'shift_name': shift_name,
        'shift_names': _shift_names(station),
    })


def _shift_detail_data(pk):
    """El turno, sus marcaciones en orden cronológico y su cadena de reaperturas."""
    shift = get_object_or_404(Shift.objects.select_related('station'), pk=pk)
    events = list(shift.events.defer('photo').order_by('event_time', 'pk'))
    return shift, events


@capability_required('shifts')
def shift_detail(request, pk):
    """Todo lo de un turno: cuándo y cómo se abrió y cerró, y quién marcó, a qué hora."""
    shift, events = _shift_detail_data(pk)
    Person.attach_photos_to_events(events)

    # La apertura original y sus reaperturas son el mismo servicio: se enlazan entre sí
    root_uid = shift.reopened_from_uid or shift.uid
    related = []
    if root_uid:
        related = list(Shift.objects.filter(station=shift.station)
                       .filter(Q(uid=root_uid) | Q(reopened_from_uid=root_uid))
                       .exclude(pk=shift.pk).order_by('started_at'))

    ok = [e for e in events if e.status == AccessEvent.Status.OK]
    return render(request, 'reports/shift_detail.html', {
        'shift': shift,
        'events': events,
        'related': related,
        'n_ok': len(ok),
        'n_personas': len({e.employee_no for e in ok}),
        'n_dup': sum(1 for e in events if e.status == AccessEvent.Status.DUPLICADO),
        'n_denied': sum(1 for e in events if e.status == AccessEvent.Status.NO_AUTORIZADO),
        'n_visitas': sum(1 for e in ok if e.is_visitor),
    })


@capability_required('shifts')
def shift_excel(request, pk):
    shift, events = _shift_detail_data(pk)
    resp = HttpResponse(
        build_shift_excel(shift, events),
        content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    )
    inicio = timezone.localtime(shift.started_at)
    resp['Content-Disposition'] = f'attachment; filename="Turno_{inicio:%Y%m%d_%H%M}.xlsx"'
    return resp
