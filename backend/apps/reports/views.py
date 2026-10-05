from datetime import datetime, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from backend.apps.core.models import AccessEvent, Person, Shift, Station
from backend.apps.webapp.permissions import capability_required
from backend.apps.reports.excel import build_excel, build_shift_excel
from backend.apps.reports.forms import MailSettingsForm, ScheduledReportForm, TestMailForm
from backend.apps.reports.mailing import MailError, retry_delivery, send_now, send_test_email
from backend.apps.reports.models import MailSettings, ReportDelivery, ScheduledReport
from backend.apps.reports.pdf import build_pdf
from backend.apps.reports.service import build_report, local_range, report_title, shift_names


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
    dt_from, dt_to = local_range(d_from, d_to)
    return d_from, d_to, dt_from, dt_to, station, shift_name


@capability_required('reports')
def report_view(request):
    d_from, d_to, dt_from, dt_to, station, shift_name = _resolve_params(request)
    data = build_report(dt_from, dt_to, station=station, shift_name=shift_name)

    context = {
        'data': data,
        'titulo': report_title(d_from, d_to, shift_name),
        'd_from': d_from,
        'd_to': d_to,
        'station': station,
        'stations': Station.objects.all(),
        'shift_name': shift_name,
        'shift_names': shift_names(station),
    }
    return render(request, 'reports/report.html', context)


@capability_required('reports')
def report_pdf(request):
    d_from, d_to, dt_from, dt_to, station, shift_name = _resolve_params(request)
    data = build_report(dt_from, dt_to, station=station, shift_name=shift_name)
    pdf = build_pdf(data, report_title(d_from, d_to, shift_name))

    resp = HttpResponse(pdf, content_type='application/pdf')
    fname = f'Informe_{d_from:%Y%m%d}_{d_to:%Y%m%d}.pdf'
    resp['Content-Disposition'] = f'attachment; filename="{fname}"'
    return resp


@capability_required('reports')
def report_excel(request):
    d_from, d_to, dt_from, dt_to, station, shift_name = _resolve_params(request)
    data = build_report(dt_from, dt_to, station=station, shift_name=shift_name)
    xlsx = build_excel(data, report_title(d_from, d_to, shift_name))

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

    dt_from, dt_to = local_range(d_from, d_to)
    shifts = (Shift.objects
              .filter(started_at__gte=dt_from, started_at__lt=dt_to)
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
        'shift_names': shift_names(station),
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


# =====================================================================
#  Envíos por correo: envíos programados, historial y servidor SMTP
# =====================================================================
def _mail_alerts(cfg, any_active):
    """Avisos de lo que impediría que los envíos salgan."""
    alerts = []
    if not cfg.is_configured:
        alerts.append('Falta configurar el servidor de correo: mientras tanto, los envíos fallan.')
    elif cfg.password_unreadable:
        alerts.append('La contraseña del servidor de correo ya no se puede leer: vuelve a '
                      'ingresarla en Servidor de correo.')
    if any_active and not cfg.scheduler_running:
        alerts.append('El programador de envíos no está corriendo (servicio «scheduler» de '
                      'docker-compose): los envíos programados no saldrán hasta que se inicie. '
                      '«Enviar ahora» funciona igual.')
    return alerts


@capability_required('report_mail')
def mail_schedule_list(request):
    latest = ReportDelivery.objects.order_by('-scheduled_for', '-id')
    reports = list(ScheduledReport.objects.select_related('station')
                   .prefetch_related(Prefetch('deliveries', queryset=latest[:1], to_attr='latest')))
    cfg = MailSettings.load()
    return render(request, 'reports/mail/schedule_list.html', {
        'reports': reports,
        'recent': ReportDelivery.objects.select_related('requested_by')[:10],
        'cfg': cfg,
        'alerts': _mail_alerts(cfg, any(r.is_active for r in reports)),
    })


def _schedule_form(request, obj=None):
    form = ScheduledReportForm(request.POST or None, instance=obj)
    if request.method == 'POST' and form.is_valid():
        report = form.save(commit=False)
        if obj is None:
            report.created_by = request.user
        report.save()
        if report.next_run_at:
            when = timezone.localtime(report.next_run_at)
            messages.success(request, f'Envío «{report.name}» guardado. Próximo envío: '
                                      f'{when:%d-%m-%Y %H:%M} ({report.next_period_text}).')
        else:
            messages.success(request, f'Envío «{report.name}» guardado (en pausa).')
        return redirect('reports:mail_schedule_list')
    return render(request, 'reports/mail/schedule_form.html', {
        'form': form, 'is_new': obj is None, 'obj': obj,
    })


@capability_required('report_mail')
def mail_schedule_create(request):
    return _schedule_form(request)


@capability_required('report_mail')
def mail_schedule_edit(request, pk):
    return _schedule_form(request, get_object_or_404(ScheduledReport, pk=pk))


@capability_required('report_mail')
def mail_schedule_delete(request, pk):
    report = get_object_or_404(ScheduledReport, pk=pk)
    if request.method == 'POST':
        report.delete()   # el historial de envíos se conserva
        messages.success(request, f'Envío «{report.name}» eliminado.')
        return redirect('reports:mail_schedule_list')
    return render(request, 'webapp/confirm_delete.html', {
        'obj': report, 'tipo': 'envío programado', 'volver': 'reports:mail_schedule_list'})


@capability_required('report_mail')
@require_POST
def mail_schedule_send(request, pk):
    """«Enviar ahora»: el período que cubriría un envío hecho hoy, sin esperar al programador."""
    report = get_object_or_404(ScheduledReport, pk=pk)
    delivery = send_now(report, user=request.user)
    if delivery.status == ReportDelivery.Status.SENT:
        messages.success(request, f'Informe {delivery.period_text} enviado a '
                                  f'{len(delivery.recipient_list)} destinatario(s).')
    else:
        messages.error(request, f'No se pudo enviar «{report.name}»: {delivery.error}')
    return redirect('reports:mail_schedule_list')


@capability_required('report_mail')
def mail_delivery_list(request):
    deliveries = ReportDelivery.objects.select_related('requested_by')
    report = None
    if request.GET.get('envio', '').isdigit():
        report = ScheduledReport.objects.filter(pk=request.GET['envio']).first()
        if report is not None:
            deliveries = deliveries.filter(report=report)
    status = request.GET.get('estado', '')
    if status in ReportDelivery.Status.values:
        deliveries = deliveries.filter(status=status)
    page = Paginator(deliveries, 50).get_page(request.GET.get('page'))
    return render(request, 'reports/mail/delivery_list.html', {
        'page': page, 'report': report, 'status': status,
        'reports': ScheduledReport.objects.all(), 'statuses': ReportDelivery.Status.choices,
    })


@capability_required('report_mail')
@require_POST
def mail_delivery_retry(request, pk):
    # solo los fallidos: los que siguen en cola los reintenta el programador
    delivery = get_object_or_404(ReportDelivery, pk=pk, status=ReportDelivery.Status.FAILED)
    if retry_delivery(delivery):
        messages.success(request, f'«{delivery.report_name}» ({delivery.period_text}) reenviado.')
    else:
        messages.error(request, f'No se pudo enviar «{delivery.report_name}»: {delivery.error}')
    back = request.POST.get('next', '')
    if not url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        back = 'reports:mail_delivery_list'
    return redirect(back)


@capability_required('mail_server')
def mail_settings(request):
    cfg = MailSettings.load()
    form = MailSettingsForm(request.POST or None, instance=cfg)
    if request.method == 'POST' and form.is_valid():
        obj = form.save(commit=False)
        obj.updated_by = request.user
        obj.save()
        messages.success(request, 'Servidor de correo guardado. Envía un correo de prueba para '
                                  'confirmar que funciona.')
        return redirect('reports:mail_settings')
    return render(request, 'reports/mail/settings.html', {
        'form': form, 'cfg': cfg,
        'test_form': TestMailForm(initial={'to': request.user.email}),
    })


@capability_required('mail_server')
@require_POST
def mail_settings_test(request):
    form = TestMailForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Indica una dirección de correo válida para la prueba.')
        return redirect('reports:mail_settings')
    to = form.cleaned_data['to']
    try:
        send_test_email(MailSettings.load(), to)
    except MailError as exc:
        messages.error(request, f'No se pudo enviar el correo de prueba: {exc}')
    else:
        messages.success(request, f'Correo de prueba enviado a {to}. Revisa la bandeja de '
                                  'entrada (y la carpeta de spam).')
    return redirect('reports:mail_settings')
