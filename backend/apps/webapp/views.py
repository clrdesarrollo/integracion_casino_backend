from collections import Counter
from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date

from django.http import Http404, HttpResponse, JsonResponse

from backend.apps.core.models import (
    AccessEvent, Person, ShiftSchedule, Station, StationAPIKey, VisitorCard,
)
from backend.apps.realtime.broadcast import active_shifts
from backend.apps.webapp.permissions import (
    DENIED_MESSAGE, admin_required, tickets_required,
)
from backend.apps.webapp.forms import (
    ShiftOvertimeForm, ShiftScheduleForm, StationForm, UserForm, VisitorCardForm,
)

User = get_user_model()


# =====================================================================
#  Dashboard
# =====================================================================
@login_required
def dashboard(request):
    # Esta es la raíz del sitio, o sea la pantalla de entrada tras iniciar sesión.
    # Quien no tenga el panel entre sus permisos no está "sin autorización": se le
    # lleva a su propia sección de entrada, sin mensaje de error.
    if not request.user.can_see_dashboard:
        home = request.user.home_url_name
        if home is None:
            raise PermissionDenied(DENIED_MESSAGE)
        return redirect(home)

    today = timezone.localdate()
    month_start = today.replace(day=1)
    tz = timezone.get_current_timezone()
    dt_month = timezone.make_aware(datetime.combine(month_start, time.min), tz)

    servidas_qs = AccessEvent.objects.filter(
        event_time__gte=dt_month, status=AccessEvent.Status.OK,
    )

    por_estacion = (
        servidas_qs.values('station__name')
        .annotate(total=Count('id')).order_by('-total')
    )

    context = {
        'total_mes': servidas_qs.count(),
        'total_estaciones': Station.objects.count(),
        'total_personas': Person.objects.count(),
        'total_eventos': AccessEvent.objects.count(),
        'por_estacion': por_estacion,
        'estaciones': Station.objects.all(),
        'ultimos_eventos': AccessEvent.objects.select_related('station')[:15],
        'mes_actual': month_start,
        'ahora': timezone.localtime(),
        'umbral_offline': timezone.now() - timedelta(hours=24),
    }
    return render(request, 'webapp/dashboard.html', context)


@tickets_required
def monitor(request):
    """Monitor de colaciones en vivo (WebSocket): réplica de la pantalla del kiosco."""
    return render(request, 'webapp/monitor.html', {
        'stations': Station.objects.all(),
    })


@tickets_required
def monitor_shifts(request):
    """Estado de turno de cada estación (lo consulta el monitor cada pocos segundos)."""
    return JsonResponse({'shifts': active_shifts()})


# =====================================================================
#  Control de colaciones de visitas (pagos adicionales)
# =====================================================================
@tickets_required
def visitor_events(request):
    """
    Marcaciones hechas con tarjeta de visita, con la foto tomada al marcar.

    Son colaciones que se facturan aparte, así que la pantalla está pensada para
    control: se ve quién retiró (foto), con qué tarjeta, en qué turno y si la
    marcación fue válida, repetida o no autorizada.
    """
    today = timezone.localdate()
    date_from = parse_date(request.GET.get('desde') or '') or today - timedelta(days=6)
    date_to = parse_date(request.GET.get('hasta') or '') or today
    if date_from > date_to:
        date_from, date_to = date_to, date_from

    tz = timezone.get_current_timezone()
    dt_from = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    dt_to = timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min), tz)

    events = (AccessEvent.objects
              .filter(is_visitor=True, event_time__gte=dt_from, event_time__lt=dt_to)
              .select_related('station', 'shift'))

    station_id = request.GET.get('estacion') or ''
    if station_id.isdigit():
        events = events.filter(station_id=int(station_id))

    status = request.GET.get('estado') or ''
    if status in AccessEvent.Status.values:
        events = events.filter(status=status)

    query = (request.GET.get('q') or '').strip()
    if query:
        events = events.filter(
            Q(person_name__icontains=query) | Q(card_no__icontains=query),
        )

    events = list(events[:500])

    # Tarjetas del set, para poner nombre a la tarjeta aunque la etiqueta haya cambiado
    labels = {
        (c.station_id, c.card_no): c.label
        for c in VisitorCard.objects.all()
    }
    for ev in events:
        ev.card_label = labels.get((ev.station_id, ev.card_no), '')

    cobrables = [e for e in events if e.status == AccessEvent.Status.OK]
    por_tarjeta = Counter(
        (e.card_no, e.card_label or e.person_name) for e in cobrables
    ).most_common()

    context = {
        'events': events,
        'stations': Station.objects.all(),
        'statuses': AccessEvent.Status.choices,
        'desde': date_from,
        'hasta': date_to,
        'estacion': station_id,
        'estado': status,
        'q': query,
        'total_cobrables': len(cobrables),
        'total_repetidas': sum(1 for e in events if e.status == AccessEvent.Status.DUPLICADO),
        'total_rechazadas': sum(1 for e in events if e.status == AccessEvent.Status.NO_AUTORIZADO),
        'tarjetas_usadas': len({e.card_no for e in cobrables}),
        'por_tarjeta': por_tarjeta,
        'truncado': len(events) >= 500,
    }
    return render(request, 'webapp/visitors/events.html', context)


@tickets_required
def visitor_event_photo(request, pk):
    """Foto tomada al marcar (solo marcaciones de visita)."""
    event = get_object_or_404(AccessEvent, pk=pk, is_visitor=True)
    if not event.photo:
        raise Http404('La marcación no tiene foto.')
    # Las fotos son inmutables: se pueden cachear en el navegador.
    response = HttpResponse(bytes(event.photo), content_type='image/jpeg')
    response['Cache-Control'] = 'private, max-age=86400'
    return response


# =====================================================================
#  Administración de usuarios
# =====================================================================
@admin_required
def user_list(request):
    return render(request, 'webapp/users/list.html', {'users': User.objects.all()})


@admin_required
def user_create(request):
    form = UserForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Usuario creado.')
        return redirect('webapp:user_list')
    return render(request, 'webapp/users/form.html', {'form': form, 'is_new': True})


@admin_required
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = UserForm(request.POST or None, instance=user)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Usuario actualizado.')
        return redirect('webapp:user_list')
    return render(request, 'webapp/users/form.html',
                  {'form': form, 'is_new': False, 'obj': user})


@admin_required
def user_delete(request, pk):
    user = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        if user.pk == request.user.pk:
            messages.error(request, 'No puedes eliminar tu propia cuenta.')
        else:
            user.delete()
            messages.success(request, 'Usuario eliminado.')
        return redirect('webapp:user_list')
    return render(request, 'webapp/confirm_delete.html',
                  {'obj': user, 'tipo': 'usuario', 'volver': 'webapp:user_list'})


# =====================================================================
#  Estaciones y API keys
# =====================================================================
@admin_required
def station_list(request):
    stations = Station.objects.all()
    return render(request, 'webapp/stations/list.html', {'stations': stations})


@admin_required
def station_create(request):
    form = StationForm(request.POST or None,
                       initial={'device_id': Station.next_device_id()})
    if request.method == 'POST' and form.is_valid():
        station = form.save()
        messages.success(request, 'Estación creada. Enrola el terminal con el ID y la contraseña.')
        return redirect('webapp:station_detail', pk=station.pk)
    return render(request, 'webapp/stations/form.html', {'form': form, 'is_new': True})


@admin_required
def station_detail(request, pk):
    station = get_object_or_404(Station, pk=pk)
    form = StationForm(request.POST or None, instance=station)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Estación actualizada.')
        return redirect('webapp:station_detail', pk=station.pk)

    context = {
        'station': station,
        'form': form,
        'api_keys': station.api_keys.all(),
        'new_key': request.session.pop('new_api_key', None),
    }
    return render(request, 'webapp/stations/detail.html', context)


@admin_required
def station_api_key_create(request, pk):
    station = get_object_or_404(Station, pk=pk)
    if request.method == 'POST':
        name = request.POST.get('name') or f'{station.name} key'
        api_key_obj, key = StationAPIKey.objects.create_key(name=name, station=station)
        # La clave en claro solo se muestra una vez.
        request.session['new_api_key'] = {'prefix': api_key_obj.prefix, 'key': key}
        messages.success(request, 'API key generada. Cópiala ahora: no se volverá a mostrar.')
    return redirect('webapp:station_detail', pk=station.pk)


@admin_required
def station_api_key_revoke(request, pk, key_id):
    station = get_object_or_404(Station, pk=pk)
    if request.method == 'POST':
        api_key = get_object_or_404(StationAPIKey, pk=key_id, station=station)
        api_key.revoked = True
        api_key.save(update_fields=['revoked'])
        messages.success(request, 'API key revocada.')
    return redirect('webapp:station_detail', pk=station.pk)


# =====================================================================
#  Turnos programados y empresas autorizadas (configuración compartida)
# =====================================================================
@admin_required
def config_index(request):
    """Punto de entrada del menú: con una sola estación va directo a sus turnos."""
    stations = list(Station.objects.all())
    if len(stations) == 1:
        return redirect('webapp:schedule_list', pk=stations[0].pk)
    return render(request, 'webapp/config/index.html', {'stations': stations})


@admin_required
def schedule_list(request, pk):
    station = get_object_or_404(Station, pk=pk)
    # La prórroga de cierre se edita en esta misma página (es configuración compartida).
    overtime_form = ShiftOvertimeForm(request.POST or None, instance=station)
    if request.method == 'POST' and overtime_form.is_valid():
        overtime_form.save()
        messages.success(
            request,
            'Prórroga actualizada. El terminal la aplicará en su próxima sincronización.',
        )
        return redirect('webapp:schedule_list', pk=station.pk)

    schedules = station.schedules.prefetch_related('companies').order_by('start_min', 'id')
    return render(request, 'webapp/config/schedule_list.html', {
        'station': station,
        'schedules': schedules,
        'stations': Station.objects.all(),
        'known_companies': station.known_companies(),
        'overtime_form': overtime_form,
    })


@admin_required
def schedule_create(request, pk):
    station = get_object_or_404(Station, pk=pk)
    form = ShiftScheduleForm(request.POST or None, station=station,
                             initial={'start': '12:00', 'end': '14:00'})
    if request.method == 'POST' and form.is_valid():
        obj = form.save(station)
        messages.success(request, f'Turno «{obj.name}» creado. El terminal lo tomará en su próxima sincronización.')
        return redirect('webapp:schedule_list', pk=station.pk)
    return render(request, 'webapp/config/schedule_form.html',
                  {'station': station, 'form': form, 'is_new': True})


@admin_required
def schedule_edit(request, pk, sid):
    station = get_object_or_404(Station, pk=pk)
    schedule = get_object_or_404(ShiftSchedule, pk=sid, station=station)
    form = ShiftScheduleForm(request.POST or None, station=station, instance=schedule)
    if request.method == 'POST' and form.is_valid():
        form.save(station)
        messages.success(request, f'Turno «{schedule.name}» actualizado. El terminal lo tomará en su próxima sincronización.')
        return redirect('webapp:schedule_list', pk=station.pk)
    return render(request, 'webapp/config/schedule_form.html',
                  {'station': station, 'form': form, 'is_new': False, 'obj': schedule})


@admin_required
def schedule_delete(request, pk, sid):
    station = get_object_or_404(Station, pk=pk)
    schedule = get_object_or_404(ShiftSchedule, pk=sid, station=station)
    if request.method == 'POST':
        name = schedule.name
        schedule.delete()
        station.touch_config()
        messages.success(request, f'Turno «{name}» eliminado.')
        return redirect('webapp:schedule_list', pk=station.pk)
    return render(request, 'webapp/confirm_delete.html', {
        'obj': schedule, 'tipo': 'turno programado',
        'volver': 'webapp:schedule_list', 'volver_pk': station.pk,
    })


# =====================================================================
#  Personas y colación asignada (solo lectura: la administra HikCentral)
# =====================================================================
@admin_required
def person_list(request, pk):
    """
    Ficha de personas de la estación con la colación asignada en HikCentral (campo
    personalizado «Colacion»: 0 = sin colación, 1 = solo almuerzo, 2 = todos los turnos).
    No se edita aquí: se cambia en HikCentral y el terminal la trae al sincronizar personas.
    """
    station = get_object_or_404(Station, pk=pk)
    query = (request.GET.get('q') or '').strip()
    policy = (request.GET.get('colacion') or '').strip()   # '', '0', '1', '2', 'none'

    persons = station.persons.exclude(user_type='visitor').order_by('name', 'employee_no')
    if query:
        persons = persons.filter(
            Q(name__icontains=query) | Q(employee_no__icontains=query) | Q(company__icontains=query),
        )
    if policy == 'none':
        persons = persons.filter(meal_policy__isnull=True)
    elif policy in ('0', '1', '2'):
        persons = persons.filter(meal_policy=int(policy))

    base = station.persons.exclude(user_type='visitor')
    counts = {
        'total': base.count(),
        'none': base.filter(meal_policy__isnull=True).count(),
    }
    for value in Person.MealPolicy.values:
        counts[str(value)] = base.filter(meal_policy=value).count()

    return render(request, 'webapp/config/person_list.html', {
        'station': station,
        'stations': Station.objects.all(),
        'persons': persons,
        'query': query,
        'policy': policy,
        'counts': counts,
        'policies': Person.MealPolicy.choices,
    })


# =====================================================================
#  Tarjetas RFID de visitas
# =====================================================================
@admin_required
def visitor_card_list(request, pk):
    station = get_object_or_404(Station, pk=pk)
    form = VisitorCardForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        no = form.cleaned_data['card_no']
        if station.visitor_cards.filter(card_no=no).exists():
            form.add_error('card_no', f'La tarjeta {no} ya está en el set.')
        else:
            label = form.cleaned_data['label'].strip() or f'Visita {station.visitor_cards.count() + 1:02d}'
            VisitorCard.objects.create(
                station=station, card_no=no, label=label,
                enabled=form.cleaned_data['enabled'],
            )
            station.touch_config()
            messages.success(request, f'Tarjeta {no} agregada como «{label}».')
            return redirect('webapp:visitor_card_list', pk=station.pk)
    return render(request, 'webapp/config/visitor_cards.html', {
        'station': station,
        'cards': station.visitor_cards.all(),
        'form': form,
        'stations': Station.objects.all(),
    })


@admin_required
def visitor_card_toggle(request, pk, cid):
    station = get_object_or_404(Station, pk=pk)
    card = get_object_or_404(VisitorCard, pk=cid, station=station)
    if request.method == 'POST':
        card.enabled = not card.enabled
        card.save(update_fields=['enabled'])
        station.touch_config()
        estado = 'habilitada' if card.enabled else 'deshabilitada'
        messages.success(request, f'Tarjeta {card.card_no} {estado}.')
    return redirect('webapp:visitor_card_list', pk=station.pk)


@admin_required
def visitor_card_delete(request, pk, cid):
    station = get_object_or_404(Station, pk=pk)
    card = get_object_or_404(VisitorCard, pk=cid, station=station)
    if request.method == 'POST':
        no = card.card_no
        card.delete()
        station.touch_config()
        messages.success(request, f'Tarjeta {no} eliminada del set.')
    return redirect('webapp:visitor_card_list', pk=station.pk)
