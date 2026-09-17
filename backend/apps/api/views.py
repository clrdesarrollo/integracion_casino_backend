import base64
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
from backend.apps.core.models import AccessEvent, Person, Shift, Station, StationAPIKey
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

        with transaction.atomic():
            for p in data['persons']:
                _, created = Person.objects.update_or_create(
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
        return Response(body, status=status.HTTP_200_OK)
