from collections import Counter, defaultdict
from datetime import datetime, time, timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from django.http import Http404, HttpResponse, JsonResponse

from backend.apps.core.models import (
    AccessEvent, Person, PersonPhoto, Role, Shift, ShiftSchedule, Station, StationAPIKey, Visit,
    VisitorCard,
)
from backend.apps.core.access import CAPABILITIES
from backend.apps.realtime.broadcast import active_shifts
from backend.apps.webapp.permissions import (
    DENIED_MESSAGE, capability_required,
)
from backend.apps.webapp.forms import (
    RoleForm, ShiftOvertimeForm, ShiftScheduleForm, StationForm, UserForm, VisitForm, VisitorCardForm,
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
    if not request.user.has_cap('dashboard'):
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

    # Colaciones de ingreso manual (turnos sin marcación): cuentan igual que las servidas.
    manuales_qs = Shift.objects.filter(started_at__gte=dt_month, manual_count__gt=0)

    totales = defaultdict(int)
    for row in servidas_qs.values('station__name').annotate(total=Count('id')):
        totales[row['station__name']] += row['total']
    for row in manuales_qs.values('station__name').annotate(total=Sum('manual_count')):
        totales[row['station__name']] += row['total'] or 0
    por_estacion = sorted(({'station__name': k, 'total': v} for k, v in totales.items()),
                          key=lambda r: r['total'], reverse=True)

    context = {
        'total_mes': servidas_qs.count() + (manuales_qs.aggregate(t=Sum('manual_count'))['t'] or 0),
        'total_estaciones': Station.objects.count(),
        'total_personas': Person.objects.count(),
        'total_eventos': AccessEvent.objects.count(),
        'por_estacion': por_estacion,
        'estaciones': Station.objects.all(),
        'ultimos_eventos': Person.attach_photos_to_events(
            AccessEvent.objects.select_related('station').defer('photo')[:15]),
        'mes_actual': month_start,
        'ahora': timezone.localtime(),
        'umbral_offline': timezone.now() - timedelta(hours=24),
    }
    return render(request, 'webapp/dashboard.html', context)


@capability_required('monitor')
def monitor(request):
    """Monitor de colaciones en vivo (WebSocket): réplica de la pantalla del kiosco."""
    return render(request, 'webapp/monitor.html', {
        'stations': Station.objects.all(),
    })


@capability_required('monitor')
def monitor_shifts(request):
    """Estado de turno de cada estación (lo consulta el monitor cada pocos segundos)."""
    return JsonResponse({'shifts': active_shifts()})


# =====================================================================
#  Control de colaciones de visitas (pagos adicionales)
# =====================================================================
@capability_required('visit_events')
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
        # Prefiltro en la base (el corte a 500 va después): por nombre/nº de tarjeta, por la
        # etiqueta del inventario o por la visita registrada con esa tarjeta. Como la visita se
        # resuelve por instante, abajo se afina en memoria una vez adjuntadas las visitas.
        visit_cards = Visit.objects.filter(
            Q(visitor_name__icontains=query) | Q(host_name__icontains=query)
            | Q(host_company__icontains=query),
        ).values_list('card_no', flat=True)
        label_cards = VisitorCard.objects.filter(label__icontains=query).values_list('card_no', flat=True)
        events = events.filter(
            Q(person_name__icontains=query) | Q(card_no__icontains=query)
            | Q(card_no__in=visit_cards) | Q(card_no__in=label_cards),
        )

    events = list(events[:500])

    # Tarjetas del set, para poner nombre a la tarjeta aunque la etiqueta haya cambiado
    labels = {
        (c.station_id, c.card_no): c.label
        for c in VisitorCard.objects.all()
    }
    for ev in events:
        ev.card_label = labels.get((ev.station_id, ev.card_no), '')
    # a quién se le había entregado la tarjeta en ese momento (registro de visitas)
    Visit.attach_to_events(events)
    if query:
        # el buscador también encuentra por la visita registrada (nombre o a quién visitaba)
        q = query.lower()
        events = [e for e in events
                  if q in (e.person_name or '').lower() or q in (e.card_no or '').lower()
                  or q in (e.card_label or '').lower()
                  or (e.visit is not None and (
                      q in e.visit.visitor_name.lower() or q in e.visit.host_display.lower()))]

    cobrables = [e for e in events if e.status == AccessEvent.Status.OK]
    por_tarjeta = Counter(
        (e.card_no, e.card_label or e.person_name) for e in cobrables
    ).most_common()
    sin_registro = sum(1 for e in cobrables if e.visit is None)

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
        'sin_registro': sin_registro,
        'truncado': len(events) >= 500,
    }
    return render(request, 'webapp/visitors/events.html', context)


@capability_required('visit_events')
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
#  Registro de visitas: entrega de tarjetas
# =====================================================================
def _visit_station(request, stations):
    """Estación elegida (POST `station` / GET `estacion`); con una sola, esa."""
    raw = request.POST.get('station') if request.method == 'POST' else request.GET.get('estacion')
    if raw and str(raw).isdigit():
        for st in stations:
            if st.pk == int(raw):
                return st
    return stations[0] if stations else None


def _visit_meal_counts(visits):
    """Colaciones cobradas (Ok) con la tarjeta mientras estuvo en manos de cada visita."""
    visits = list(visits)
    if not visits:
        return {}
    events = list(AccessEvent.objects.filter(
        is_visitor=True, status=AccessEvent.Status.OK,
        station_id__in={v.station_id for v in visits},
        card_no__in={v.card_no for v in visits},
        event_time__gte=min(v.delivered_at for v in visits),
    ))
    Visit.attach_to_events(events)
    return Counter(e.visit.pk for e in events if e.visit is not None)


@capability_required('visits')
def visit_list(request):
    """
    Registro de visitas: a quién se entregó cada tarjeta, quién la entregó (el usuario
    que registra) y a quién venía a ver. Quien registra puede ser cualquiera que vea las
    colaciones (portería, casino, gerencia): es un trámite de mesón, no configuración.
    """
    stations = list(Station.objects.all())
    station = _visit_station(request, stations)
    if station is None:
        messages.warning(request, 'Aún no hay estaciones: crea una y enrola el terminal '
                                  'antes de registrar visitas.')
        return render(request, 'webapp/visitors/visits.html', {'stations': stations, 'station': None})

    form = VisitForm(request.POST or None, station=station)
    if request.method == 'POST' and form.is_valid():
        visit = form.save(user=request.user)
        messages.success(
            request,
            f'Tarjeta «{visit.card_display}» entregada a {visit.visitor_name}'
            + (f' (visita a {visit.host_display})' if visit.host_display else '') + '.',
        )
        return redirect(f"{reverse('webapp:visit_list')}?estacion={station.pk}")

    today = timezone.localdate()
    date_from = parse_date(request.GET.get('desde') or '') or today - timedelta(days=30)
    date_to = parse_date(request.GET.get('hasta') or '') or today
    if date_from > date_to:
        date_from, date_to = date_to, date_from
    tz = timezone.get_current_timezone()
    dt_from = timezone.make_aware(datetime.combine(date_from, time.min), tz)
    dt_to = timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min), tz)

    open_visits = list(Visit.objects.open().select_related('station').order_by('delivered_at'))

    history = (Visit.objects.filter(delivered_at__gte=dt_from, delivered_at__lt=dt_to)
               .select_related('station'))
    if len(stations) > 1 and request.GET.get('estacion'):
        history = history.filter(station=station)
    query = (request.GET.get('q') or '').strip()
    if query:
        history = history.filter(
            Q(visitor_name__icontains=query) | Q(visitor_document__icontains=query)
            | Q(host_name__icontains=query) | Q(host_company__icontains=query)
            | Q(card_no__icontains=query) | Q(card_label__icontains=query)
            | Q(delivered_by_name__icontains=query),
        )
    history = list(history[:500])

    meals = _visit_meal_counts(open_visits + history)
    for v in open_visits + history:
        v.meals = meals.get(v.pk, 0)

    return render(request, 'webapp/visitors/visits.html', {
        'form': form,
        'station': station,
        'stations': stations,
        'companies': station.known_companies(),
        'open_visits': open_visits,
        'history': history,
        'cards_total': station.visitor_cards.filter(enabled=True).count(),
        'cards_in_use': sum(1 for v in open_visits if v.station_id == station.pk),
        'desde': date_from,
        'hasta': date_to,
        'q': query,
        'truncado': len(history) >= 500,
    })


@capability_required('visits')
def visit_return(request, pk):
    """La visita devolvió la tarjeta: desde ahora sus colaciones ya no se le atribuyen."""
    visit = get_object_or_404(Visit, pk=pk)
    if request.method == 'POST':
        if visit.is_open:
            visit.close(user=request.user)
            messages.success(request, f'Tarjeta «{visit.card_display}» recibida de vuelta '
                                      f'de {visit.visitor_name}.')
        else:
            messages.info(request, 'Esa tarjeta ya estaba devuelta.')
    return redirect(f"{reverse('webapp:visit_list')}?estacion={visit.station_id}")


@capability_required('visit_cards')
def visit_delete(request, pk):
    """Borrar un registro equivocado. Solo el administrador: el registro respalda el cobro."""
    visit = get_object_or_404(Visit, pk=pk)
    if request.method == 'POST':
        nombre = visit.visitor_name
        visit.delete()
        messages.success(request, f'Registro de visita de {nombre} eliminado.')
    return redirect(f"{reverse('webapp:visit_list')}?estacion={visit.station_id}")


@capability_required('visits')
def visit_person_suggest(request):
    """
    Autocompletado del funcionario visitado: personas de la estación (ficha de HikCentral)
    cuyo nombre contenga lo escrito, acotadas a la empresa si se indicó.
    """
    station_id = request.GET.get('estacion') or ''
    query = ' '.join((request.GET.get('q') or '').split())
    company = (request.GET.get('empresa') or '').strip()
    persons = Person.objects.exclude(user_type='visitor').exclude(name='')
    if station_id.isdigit():
        persons = persons.filter(station_id=int(station_id))
    if company:
        persons = persons.filter(company__iexact=company)
    for word in query.split():
        persons = persons.filter(name__icontains=word)
    rows = [{'name': p.name, 'company': p.company, 'employee_no': p.employee_no}
            for p in persons.order_by('name')[:15]]
    return JsonResponse({'results': rows})


@capability_required('visit_cards')
def visitor_cards_index(request):
    """Inventario de tarjetas físicas: con una sola estación va directo a su set."""
    stations = list(Station.objects.all())
    if len(stations) == 1:
        return redirect('webapp:visitor_card_list', pk=stations[0].pk)
    return render(request, 'webapp/visitors/cards_index.html', {'stations': stations})


# =====================================================================
@capability_required('users')
def user_list(request):
    return render(request, 'webapp/users/list.html',
                  {'users': User.objects.select_related('role')})


@capability_required('users')
def user_create(request):
    form = UserForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Usuario creado.')
        return redirect('webapp:user_list')
    return render(request, 'webapp/users/form.html', {'form': form, 'is_new': True})


@capability_required('users')
def user_edit(request, pk):
    user = get_object_or_404(User, pk=pk)
    form = UserForm(request.POST or None, instance=user)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Usuario actualizado.')
        return redirect('webapp:user_list')
    return render(request, 'webapp/users/form.html',
                  {'form': form, 'is_new': False, 'obj': user})


@capability_required('users')
def user_delete(request, pk):
    user = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        if user.pk == request.user.pk:
            messages.error(request, 'No puedes eliminar tu propia cuenta.')
        elif user.role.is_admin and not (User.objects.filter(role__is_admin=True, is_active=True)
                                         .exclude(pk=user.pk).exists()):
            messages.error(request, 'Es el único administrador activo: no se puede eliminar.')
        else:
            user.delete()
            messages.success(request, 'Usuario eliminado.')
        return redirect('webapp:user_list')
    return render(request, 'webapp/confirm_delete.html',
                  {'obj': user, 'tipo': 'usuario', 'volver': 'webapp:user_list'})


# =====================================================================
#  Roles
# =====================================================================
@capability_required('users')
def role_list(request):
    roles = Role.objects.annotate(n_users=Count('users'))
    return render(request, 'webapp/roles/list.html', {'roles': roles, 'capabilities': CAPABILITIES})


@capability_required('users')
def role_create(request):
    form = RoleForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Rol creado.')
        return redirect('webapp:role_list')
    return render(request, 'webapp/roles/form.html',
                  {'form': form, 'is_new': True, 'capabilities': CAPABILITIES})


@capability_required('users')
def role_edit(request, pk):
    role = get_object_or_404(Role, pk=pk)
    form = RoleForm(request.POST or None, instance=role)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, 'Rol actualizado.')
        return redirect('webapp:role_list')
    return render(request, 'webapp/roles/form.html',
                  {'form': form, 'is_new': False, 'obj': role, 'capabilities': CAPABILITIES})


@capability_required('users')
def role_delete(request, pk):
    role = get_object_or_404(Role, pk=pk)
    in_use = role.users.count()
    blocked = None
    if role.is_system:
        blocked = 'Los roles de sistema no se pueden eliminar.'
    elif in_use:
        blocked = (f'El rol tiene {in_use} usuario(s) asignado(s): '
                   'reasígnalos a otro rol antes de eliminarlo.')
    if request.method == 'POST':
        if blocked:
            messages.error(request, blocked)
        else:
            role.delete()
            messages.success(request, 'Rol eliminado.')
        return redirect('webapp:role_list')
    if blocked:
        messages.error(request, blocked)
        return redirect('webapp:role_list')
    return render(request, 'webapp/confirm_delete.html',
                  {'obj': role, 'tipo': 'rol', 'volver': 'webapp:role_list'})


# =====================================================================
#  Estaciones y API keys
# =====================================================================
@capability_required('config')
def station_list(request):
    stations = Station.objects.all()
    return render(request, 'webapp/stations/list.html', {'stations': stations})


@capability_required('config')
def station_create(request):
    form = StationForm(request.POST or None,
                       initial={'device_id': Station.next_device_id()})
    if request.method == 'POST' and form.is_valid():
        station = form.save()
        messages.success(request, 'Estación creada. Enrola el terminal con el ID y la contraseña.')
        return redirect('webapp:station_detail', pk=station.pk)
    return render(request, 'webapp/stations/form.html', {'form': form, 'is_new': True})


@capability_required('config')
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


@capability_required('config')
def station_api_key_create(request, pk):
    station = get_object_or_404(Station, pk=pk)
    if request.method == 'POST':
        name = request.POST.get('name') or f'{station.name} key'
        api_key_obj, key = StationAPIKey.objects.create_key(name=name, station=station)
        # La clave en claro solo se muestra una vez.
        request.session['new_api_key'] = {'prefix': api_key_obj.prefix, 'key': key}
        messages.success(request, 'API key generada. Cópiala ahora: no se volverá a mostrar.')
    return redirect('webapp:station_detail', pk=station.pk)


@capability_required('config')
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
@capability_required('config')
def config_index(request):
    """Punto de entrada del menú: con una sola estación va directo a sus turnos."""
    stations = list(Station.objects.all())
    if len(stations) == 1:
        return redirect('webapp:schedule_list', pk=stations[0].pk)
    return render(request, 'webapp/config/index.html', {'stations': stations})


@capability_required('config')
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


@capability_required('config')
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


@capability_required('config')
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


@capability_required('config')
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
@capability_required('config')
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


@login_required
def person_photo(request, pk):
    """
    Foto de perfil de una persona (la de HikCentral, respaldada por el terminal). La ve
    cualquier usuario con sesión: acompaña a las marcaciones del panel y del monitor.
    """
    photo = get_object_or_404(PersonPhoto, person_id=pk)
    # La URL lleva el hash de la foto (?v=): si cambia la foto cambia la URL, así que
    # el navegador puede cachearla sin mostrar una antigua.
    response = HttpResponse(bytes(photo.data), content_type='image/jpeg')
    response['Cache-Control'] = 'private, max-age=604800'
    return response


# =====================================================================
#  Tarjetas RFID de visitas
# =====================================================================
@capability_required('visit_cards')
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


@capability_required('visit_cards')
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


@capability_required('visit_cards')
def visitor_card_delete(request, pk, cid):
    station = get_object_or_404(Station, pk=pk)
    card = get_object_or_404(VisitorCard, pk=cid, station=station)
    if request.method == 'POST':
        no = card.card_no
        card.delete()
        station.touch_config()
        messages.success(request, f'Tarjeta {no} eliminada del set.')
    return redirect('webapp:visitor_card_list', pk=station.pk)
