import json
from datetime import datetime, timedelta

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Prefetch, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from backend.apps.core import audit
from backend.apps.core.models import AccessEvent, Person, Shift, Station
from backend.apps.webapp.permissions import capability_required
from backend.apps.reports import manual
from backend.apps.reports.excel import build_excel, build_shift_excel
from backend.apps.reports.forms import (
    AnnulEventForm, MailSettingsForm, ManualEventForm, ScheduledReportForm, TestMailForm,
)
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
                        n_denied=_event_count(AccessEvent.Status.NO_AUTORIZADO),
                        # de las válidas, las ingresadas a mano desde el backoffice
                        n_manual=Count('events', filter=Q(events__status=AccessEvent.Status.OK,
                                                          events__origin=AccessEvent.Origin.BACKOFFICE)))
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
        'n_manual': sum(1 for e in ok if e.is_manual),
        'n_anuladas': sum(1 for e in events if e.is_annulled),
        # el botón de ingreso manual solo si el usuario puede y el turno lo admite
        'can_manual': request.user.has_cap('manual_events') and manual.shift_block_reason(shift) is None,
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
#  Ingreso manual de colaciones (ver reports.manual)
# =====================================================================
def _safe_next(request, fallback):
    back = request.POST.get('next') or request.GET.get('next') or ''
    if back and url_has_allowed_host_and_scheme(back, allowed_hosts={request.get_host()}):
        return back
    return fallback


@capability_required('manual_events')
def manual_event_list(request):
    """
    Todas las colaciones ingresadas a mano (válidas y anuladas) en el período, y el
    acceso para registrar una nueva sobre un turno reciente.
    """
    today = timezone.localdate()
    d_from = _parse_date(request.GET.get('from'), today - timedelta(days=30))
    d_to = _parse_date(request.GET.get('to'), today)
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    dt_from, dt_to = local_range(d_from, d_to)

    events = (AccessEvent.objects
              .filter(origin=AccessEvent.Origin.BACKOFFICE,
                      event_time__gte=dt_from, event_time__lt=dt_to)
              .select_related('station', 'shift').defer('photo')
              .order_by('-event_time', '-pk'))
    station = None
    if request.GET.get('station'):
        station = Station.objects.filter(pk=request.GET['station']).first()
        if station is not None:
            events = events.filter(station=station)
    events = list(events)
    Person.attach_photos_to_events(events)

    # Turnos recientes en los que se puede ingresar: cerrados y con marcaciones
    since = timezone.now() - timedelta(days=7)
    recent_shifts = list(Shift.objects
                         .filter(started_at__gte=since, ended_at__isnull=False, manual_count__isnull=True)
                         .select_related('station').order_by('-started_at'))

    return render(request, 'reports/manual_list.html', {
        'events': events,
        'n_validas': sum(1 for e in events if not e.is_annulled),
        'n_anuladas': sum(1 for e in events if e.is_annulled),
        'd_from': d_from,
        'd_to': d_to,
        'station': station,
        'stations': Station.objects.all(),
        'recent_shifts': recent_shifts,
        'annul_form': AnnulEventForm(),
    })


@capability_required('manual_events')
def manual_event_create(request, pk):
    """
    Formulario de ingreso manual sobre UN turno. Tras registrar vuelve al mismo
    formulario (lo normal es tener una lista de varias personas), con las ya registradas
    a la vista para no repetir.
    """
    shift = get_object_or_404(Shift.objects.select_related('station'), pk=pk)
    block = manual.shift_block_reason(shift)
    window_start, window_end = manual.shift_window(shift)
    default_time = timezone.localtime(shift.ended_at or window_end).replace(microsecond=0).time()
    form = ManualEventForm(request.POST or None, initial={'event_time': default_time})

    selected = None   # la persona elegida, para volver a mostrarla si el formulario falla
    if request.method == 'POST' and block is None:
        valid = form.is_valid()
        person_id = form.cleaned_data.get('person')
        person = shift.station.persons.filter(pk=person_id).first() if person_id else None
        if valid:
            if person is None:
                form.add_error(None, 'Elige a la persona desde la búsqueda (debe estar en la ficha '
                                     'de la estación).')
            else:
                try:
                    event = manual.register_manual_event(
                        shift, person, form.cleaned_data['event_time'], form.cleaned_data['reason'],
                        request.user, override=form.cleaned_data['override'], request=request)
                except manual.ManualEntryError as exc:
                    form.add_error(None, str(exc))
                else:
                    messages.success(
                        request,
                        f'Colación de {event.person_name or event.employee_no} registrada a las '
                        f'{timezone.localtime(event.event_time):%H:%M:%S} como ingreso manual. '
                        'Puedes registrar a la siguiente persona.')
                    return redirect('reports:manual_event_create', pk=shift.pk)
        if person is not None:
            checks = manual.person_checks(shift, person, manual.served_in_service(shift))
            selected = {
                'id': person.pk, 'name': person.name or f'Nº {person.employee_no}',
                'employee_no': person.employee_no, 'company': person.company or '',
                'meal_policy': person.meal_policy_text, 'authorized': person.authorized,
                'photo_url': person.photo_url, 'blocked': checks['blocked'],
                'warnings': checks['warnings'],
            }

    manual_events = manual.manual_events_of(shift)
    Person.attach_photos_to_events(manual_events)
    return render(request, 'reports/manual_form.html', {
        'shift': shift,
        'form': form,
        'block_reason': block,
        'window_start': window_start,
        'window_end': window_end,
        'is_old': manual.is_old(shift),
        'n_ok': shift.events.filter(status=AccessEvent.Status.OK).count(),
        'manual_events': manual_events,
        'selected_json': json.dumps(selected) if selected else 'null',
        'annul_form': AnnulEventForm(),
    })


@capability_required('manual_events')
def manual_event_persons(request, pk):
    """Búsqueda de personas de la ficha para el formulario (JSON), con sus avisos."""
    shift = get_object_or_404(Shift.objects.select_related('station'), pk=pk)
    return JsonResponse({'results': manual.search_persons(shift, request.GET.get('q', ''))})


@capability_required('manual_events')
@require_POST
def manual_event_annul(request, pk):
    """Anula un ingreso manual (la fila queda, con quién y por qué)."""
    event = get_object_or_404(
        AccessEvent.objects.defer('photo').select_related('shift', 'station'),
        pk=pk, origin=AccessEvent.Origin.BACKOFFICE)
    fallback = (reverse('reports:manual_event_create', args=[event.shift_id]) if event.shift_id
                else reverse('reports:manual_event_list'))
    form = AnnulEventForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Indica el motivo de la anulación (al menos 5 caracteres).')
        return redirect(_safe_next(request, fallback))
    try:
        manual.annul_manual_event(event, form.cleaned_data['reason'], request.user, request=request)
    except manual.ManualEntryError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f'Colación de {event.person_name or event.employee_no} anulada. '
                                  'La fila se conserva marcada como anulada.')
    return redirect(_safe_next(request, fallback))


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
        audit.log('correo.envio_guardado',
                  f'Envío programado «{report.name}» {"creado" if obj is None else "editado"}: '
                  f'{"activo" if report.is_active else "en pausa"}, destinatarios {report.recipients}.',
                  request=request)
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
        audit.log('correo.envio_eliminado', f'Envío programado «{report.name}» eliminado.',
                  request=request, level=audit.WARNING)
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
    audit.log('correo.envio_manual',
              f'«Enviar ahora» del envío «{report.name}» ({delivery.period_text}): '
              f'{delivery.get_status_display()}'
              + (f' · {delivery.error}' if delivery.error else '') + '.',
              request=request,
              level=audit.INFO if delivery.status == ReportDelivery.Status.SENT else audit.ERROR)
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
        audit.log('correo.servidor_guardado',
                  f'Servidor de correo guardado: {obj.host}:{obj.port}, remitente {obj.from_email}.',
                  request=request)
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
