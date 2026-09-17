from datetime import date, datetime, time, timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

from backend.apps.core.models import Shift, Station
from backend.apps.webapp.permissions import tickets_required
from backend.apps.reports.excel import build_excel
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


@tickets_required
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


@tickets_required
def report_pdf(request):
    d_from, d_to, dt_from, dt_to, station, shift_name = _resolve_params(request)
    data = build_report(dt_from, dt_to, station=station, shift_name=shift_name)
    pdf = build_pdf(data, _titulo(d_from, d_to, shift_name))

    resp = HttpResponse(pdf, content_type='application/pdf')
    fname = f'Informe_{d_from:%Y%m%d}_{d_to:%Y%m%d}.pdf'
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


@tickets_required
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
