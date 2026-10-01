import base64
import hashlib
import logging

from django.db import connections, transaction
from django.db.utils import OperationalError
from django.http import JsonResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from backend.apps.api.permissions import HasStationAPIKey
from backend.apps.api.serializers import EnrollSerializer, SyncSerializer
from backend.apps.core.config_sync import reconcile
from backend.apps.core.models import (
    AccessEvent, Person, PersonPhoto, Shift, Station, StationAPIKey, Visit,
)
from backend.apps.realtime.broadcast import broadcast_events

logger = logging.getLogger(__name__)


class HealthcheckView(APIView):
    """Estado del servicio (sin autenticación)."""

    permission_classes = []

    def get(self, request):
        try:
            connections['default'].cursor()
            db_ok = True
        except OperationalError:
            db_ok = False

        return JsonResponse(
            {'status': 'ok' if db_ok else 'error', 'database': db_ok},
            status=200 if db_ok else 500,
        )


class EnrollView(APIView):
    """
    Enrola un terminal CasinoAccess sin copiar API keys a mano: el terminal envía
    el ID de dispositivo y la contraseña de enrolado definidos en el backoffice
    (Estaciones) y recibe la API key con la que sincronizará.
    """

    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'enroll'

    def post(self, request):
        serializer = EnrollSerializer(data=request.data)
        if not serializer.is_valid():
            # claves y errores solamente: la contraseña jamás se registra en logs
            logger.warning(
                'Enrolado con payload inválido: claves recibidas=%s errores=%s',
                sorted(request.data.keys()) if hasattr(request.data, 'keys') else type(request.data).__name__,
                dict(serializer.errors),
            )
            return Response(
                {'validate': False, 'message': 'Payload inválido', 'errors': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        station = Station.objects.filter(device_id=data['device_id']).first()

        # Mensaje genérico: no se revela si el ID existe, está inactivo o la clave no calza.
        if (station is None or not station.is_active
                or not station.check_enroll_password(data['password'])):
            return Response(
                {'validate': False, 'message': 'ID de dispositivo o contraseña incorrectos.'},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Un enrolado nuevo reemplaza la key del enrolado anterior; las creadas
        # a mano en el backoffice no se tocan.
        station.api_keys.filter(revoked=False, name__startswith='enrolado').update(revoked=True)
        _, key = StationAPIKey.objects.create_key(
            name=f'enrolado {timezone.localtime():%d-%m-%Y %H:%M}', station=station,
        )

        return Response({
            'validate': True,
            'message': f'Terminal enrolado en la estación «{station.name}».',
            'api_key': key,
            'station': {
                'name': station.name,
                'device_id': station.device_id,
                'location': station.location,
            },
        })


def upsert_by_uid(model, station, uid, remote_id, defaults, same_as):
    """
    Alta/actualización de un turno o marcación identificándolo por su uid (estable) y
    cayendo al remote_id cuando el terminal aún no lo envía.

    Un registro respaldado ANTES de que existieran los uid tiene uid='': al llegar el
    mismo remote_id ya con uid, lo adopta en vez de crear un duplicado (que además
    chocaría con la unicidad de remote_id).

    `same_as` decide si ese registro sin uid es de verdad el mismo (comparando los datos
    que no cambian, como la hora). Es lo que distingue "este registro ya lo respaldé antes
    de la migración" de "recrearon la base del terminal y el remote_id volvió a empezar":
    sin esa comprobación, la primera marcación de la base nueva pisaría una histórica.
    """
    if not uid:
        # Solo se toca lo que tampoco tiene uid: nunca se le quita la identidad a un
        # registro que ya la tiene (y así el filtro cae en la clave única condicional).
        obj = model.objects.filter(station=station, remote_id=remote_id, uid='').first()
        if obj is None:
            return model.objects.create(
                station=station, uid='', remote_id=remote_id, **defaults,
            ), True
        for field, value in defaults.items():
            setattr(obj, field, value)
        obj.save()
        return obj, False

    obj = model.objects.filter(station=station, uid=uid).first()
    if obj is None:
        legacy = model.objects.filter(station=station, remote_id=remote_id, uid='').first()
        if legacy is not None and same_as(legacy):
            obj = legacy
    if obj is None:
        return model.objects.create(
            station=station, uid=uid, remote_id=remote_id, **defaults,
        ), True

    obj.uid = uid
    obj.remote_id = remote_id
    for field, value in defaults.items():
        setattr(obj, field, value)
    obj.save()
    return obj, False


class SyncView(APIView):
    """
    Recibe y respalda los registros de una estación CasinoAccess.

    La operación es idempotente: reenviar el mismo lote no duplica registros
    (se identifica por el id local de la estación).
    """

    permission_classes = [HasStationAPIKey]

    def post(self, request):
        serializer = SyncSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {'validate': False, 'message': 'Payload inválido', 'errors': serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        station = request.station
        data = serializer.validated_data

        result = {'persons': {'created': 0, 'updated': 0},
                  'shifts': {'created': 0, 'updated': 0},
                  'events': {'created': 0, 'updated': 0}}
        created_events = []  # solo las marcaciones nuevas se difunden al monitor en vivo
        photos_needed = []   # personas cuya foto el terminal tiene y aquí falta o es otra

        with transaction.atomic():
            for p in data['persons']:
                person, created = Person.objects.update_or_create(
                    station=station,
                    employee_no=p['employee_no'],
                    defaults={
                        'name': p['name'],
                        'user_type': p['user_type'],
                        'company': p['company'],
                        'authorized': p['authorized'],
                        'person_id': p['person_id'],
                        'meal_policy': p.get('meal_policy'),
                    },
                )
                result['persons']['created' if created else 'updated'] += 1

                # Foto de perfil: si viene la imagen se guarda (el hash se calcula acá, no
                # se confía en el declarado); si solo viene el hash y no coincide, se pide.
                # Un terminal sin foto para la persona no borra la que ya estaba respaldada.
                photo_b64 = p.get('photo_b64') or ''
                if photo_b64:
                    raw = base64.b64decode(photo_b64)
                    digest = hashlib.sha1(raw).hexdigest()
                    if digest != person.photo_hash:
                        PersonPhoto.objects.update_or_create(person=person, defaults={'data': raw})
                        Person.objects.filter(pk=person.pk).update(photo_hash=digest)
                elif p.get('photo_hash') and p['photo_hash'].lower() != person.photo_hash:
                    photos_needed.append(person.employee_no)

            # Bajas: el terminal mandó su lista completa, así que quien ya no viene fue
            # eliminado de HikCentral. Las marcaciones guardan su propia copia del nombre y
            # no se tocan. Una lista vacía no se toma como «borrar a todos».
            if data['persons_complete'] and data['persons']:
                gone = station.persons.exclude(
                    employee_no__in=[p['employee_no'] for p in data['persons']])
                result['persons']['deleted'] = gone.count()
                gone.delete()

            # El terminal leyó HikCentral: queda la hora; y si lo hizo por la orden del
            # backoffice, la solicitud se da por atendida.
            if data.get('persons_refreshed_at') is not None:
                station.persons_refreshed_at = data['persons_refreshed_at']
                station.save(update_fields=['persons_refreshed_at'])
            if data['persons_refresh_ack'] and station.persons_refresh_requested_at is not None:
                station.persons_refresh_requested_at = None
                station.save(update_fields=['persons_refresh_requested_at'])

            for s in data['shifts']:
                # El uid es la clave estable (sobrevive a que se recree la base del
                # terminal); un terminal antiguo no lo manda y se cae al remote_id.
                _, created = upsert_by_uid(
                    Shift, station, (s.get('uid') or '').strip(), s['remote_id'],
                    {
                        'name': s['name'],
                        'started_at': s['started_at'],
                        'ended_at': s.get('ended_at'),
                        'auto': s['auto'],
                        'schedule_uid': s.get('schedule_uid', ''),
                        'service_date': s.get('service_date'),
                        'end_reason': s.get('end_reason', ''),
                        'reopened_from_uid': s.get('reopened_from_uid', ''),
                        'manual_count': s.get('manual_count'),
                    },
                    # el mismo turno respaldado antes de los uid: empezó a la misma hora
                    same_as=lambda old, s=s: old.started_at == s['started_at'],
                )
                result['shifts']['created' if created else 'updated'] += 1

            # Mapas de turnos de la estación para resolver la FK de los eventos: por uid
            # (preferente) y por remote_id (terminales antiguos).
            shifts_qs = list(station.shifts.all())
            shift_by_uid = {sh.uid: sh for sh in shifts_qs if sh.uid}
            shift_by_remote = {sh.remote_id: sh for sh in shifts_qs}

            for e in data['events']:
                shift_remote_id = e.get('shift_remote_id')
                shift_uid = (e.get('shift_uid') or '').strip()
                shift_obj = shift_by_uid.get(shift_uid) if shift_uid else None
                if shift_obj is None and shift_remote_id:
                    shift_obj = shift_by_remote.get(shift_remote_id)

                defaults = {
                    'shift': shift_obj,
                    'shift_remote_id': shift_remote_id,
                    'employee_no': e['employee_no'],
                    'person_name': e['person_name'],
                    'company': e['company'],
                    'verify_method': e['verify_method'],
                    'card_no': e['card_no'],
                    'event_time': e['event_time'],
                    'status': e['status'],
                    'is_visitor': e.get('is_visitor', False),
                    'detail': e.get('detail', ''),
                }
                # La foto solo viene en marcaciones de visita; si el lote se reenvía sin
                # foto, no se borra la que ya estaba guardada.
                photo_b64 = e.get('photo_b64') or ''
                if photo_b64:
                    defaults['photo'] = base64.b64decode(photo_b64)

                obj, created = upsert_by_uid(
                    AccessEvent, station, (e.get('uid') or '').strip(), e['remote_id'], defaults,
                    # la misma marcación respaldada antes de los uid: misma persona y hora
                    same_as=lambda old, e=e: (old.event_time == e['event_time']
                                              and old.employee_no == e['employee_no']),
                )
                result['events']['created' if created else 'updated'] += 1
                if created:
                    created_events.append(obj)

            # Tarjetas de visita de un solo uso: primero las cargas creadas en el terminal
            # (deben existir antes de marcarlas) y luego las que ya se gastaron.
            Visit.register_totem_grants(station, data['card_grants_local'])
            Visit.mark_used(station, data['card_grants_used'])

            # Configuración compartida (turnos/empresas autorizadas/tarjetas de visita):
            # gana la edición más reciente. Si la del servidor es más nueva, se devuelve.
            config_out = None
            if data.get('config') is not None:
                config_out = reconcile(station, data['config'])

            station.touch_sync()

        # Difusión al monitor en vivo tras confirmar la transacción (nunca rompe la ingesta).
        if created_events:
            transaction.on_commit(
                lambda: broadcast_events(created_events, station.name),
            )

        body = {'validate': True, 'message': 'Sincronización realizada', 'result': result}
        if config_out is not None:
            body['config'] = config_out
        if photos_needed:
            body['photos_needed'] = photos_needed
        # Cargas vigentes de las tarjetas de visita: van SIEMPRE, el terminal las reemplaza
        body['card_grants'] = Visit.grants_for(station)
        # Órdenes pendientes para el terminal (respaldo del WebSocket /ws/station/)
        if station.persons_refresh_requested_at is not None:
            body['commands'] = ['sync_persons']
        return Response(body, status=status.HTTP_200_OK)
