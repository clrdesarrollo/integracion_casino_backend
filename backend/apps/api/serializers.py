import base64
import binascii

from rest_framework import serializers

from backend.apps.core.models import AccessEvent

# La estación (CasinoAccess) guarda las horas en hora local con este formato.
DATETIME_INPUT_FORMATS = ['iso-8601', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']


def _clean_photo_b64(value):
    """Se descarta una foto ilegible o desmedida en vez de rechazar todo el lote."""
    if not value:
        return ''
    # ~4/3 del tamaño binario: 1.5 MB de base64 ≈ 1.1 MB de imagen, de sobra para un JPEG
    if len(value) > 1_500_000:
        return ''
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return ''
    return value


class EnrollSerializer(serializers.Serializer):
    """Credenciales de enrolado que envía el terminal (ID numérico + contraseña)."""

    device_id = serializers.IntegerField(min_value=0)
    password = serializers.CharField(max_length=128)


class PersonSyncSerializer(serializers.Serializer):
    employee_no = serializers.CharField(max_length=100)
    name = serializers.CharField(max_length=200, allow_blank=True, required=False, default='')
    user_type = serializers.CharField(max_length=20, required=False, default='normal')
    company = serializers.CharField(max_length=200, allow_blank=True, required=False, default='')
    authorized = serializers.BooleanField(required=False, default=True)
    person_id = serializers.CharField(max_length=100, allow_blank=True, required=False, default='')
    # Colación asignada en HikCentral (0/1/2); null = sin definir. Un terminal antiguo no la
    # manda: se guarda como sin definir.
    meal_policy = serializers.IntegerField(
        min_value=0, max_value=2, required=False, allow_null=True, default=None,
    )
    # Foto de perfil (la de HikCentral): el terminal manda siempre el SHA-1 de la que tiene
    # guardada, y la imagen solo cuando el servidor se la pidió (photos_needed). null = el
    # terminal no informa foto (versión antigua, o la persona no tiene).
    photo_hash = serializers.CharField(
        max_length=40, required=False, allow_blank=True, allow_null=True, default=None,
    )
    photo_b64 = serializers.CharField(required=False, allow_blank=True, allow_null=True, default='')

    def validate_photo_b64(self, value):
        return _clean_photo_b64(value)


class ShiftSyncSerializer(serializers.Serializer):
    remote_id = serializers.IntegerField()
    # Identidad global de la apertura. Un terminal antiguo no la manda: se cae al remote_id.
    uid = serializers.CharField(max_length=40, allow_blank=True, required=False, default='')
    name = serializers.CharField(max_length=120)
    started_at = serializers.DateTimeField(input_formats=DATETIME_INPUT_FORMATS)
    ended_at = serializers.DateTimeField(
        input_formats=DATETIME_INPUT_FORMATS, required=False, allow_null=True,
    )
    auto = serializers.BooleanField(required=False, default=False)
    schedule_uid = serializers.CharField(max_length=40, allow_blank=True, required=False, default='')
    service_date = serializers.DateField(required=False, allow_null=True)
    end_reason = serializers.CharField(max_length=20, allow_blank=True, required=False, default='')
    reopened_from_uid = serializers.CharField(
        max_length=40, allow_blank=True, required=False, default='',
    )
    # Colaciones de un registro de ingreso manual (turno sin marcaciones). null = turno normal.
    manual_count = serializers.IntegerField(
        min_value=0, required=False, allow_null=True, default=None,
    )


class EventSyncSerializer(serializers.Serializer):
    remote_id = serializers.IntegerField()
    uid = serializers.CharField(max_length=40, allow_blank=True, required=False, default='')
    shift_remote_id = serializers.IntegerField(required=False, allow_null=True)
    shift_uid = serializers.CharField(max_length=40, allow_blank=True, required=False, allow_null=True, default='')
    employee_no = serializers.CharField(max_length=100)
    person_name = serializers.CharField(max_length=200, allow_blank=True, required=False, default='')
    company = serializers.CharField(max_length=200, allow_blank=True, required=False, default='')
    verify_method = serializers.CharField(max_length=50, allow_blank=True, required=False, default='')
    card_no = serializers.CharField(max_length=100, allow_blank=True, required=False, default='')
    event_time = serializers.DateTimeField(input_formats=DATETIME_INPUT_FORMATS)
    # «Anulado» es un estado del backoffice (ingreso manual anulado): el terminal no lo manda.
    status = serializers.ChoiceField(choices=AccessEvent.TERMINAL_STATUSES)
    is_visitor = serializers.BooleanField(required=False, default=False)
    detail = serializers.CharField(max_length=300, allow_blank=True, required=False, default='')
    # Foto en base64: solo llega en marcaciones de visita (constancia del pago adicional).
    photo_b64 = serializers.CharField(required=False, allow_blank=True, allow_null=True, default='')

    def validate_photo_b64(self, value):
        return _clean_photo_b64(value)


class ScheduleConfigSerializer(serializers.Serializer):
    """Turno programado con sus reglas (empresas autorizadas / visitas)."""

    remote_id = serializers.IntegerField(required=False, allow_null=True)
    uid = serializers.CharField(max_length=40, allow_blank=True, required=False, default='')
    name = serializers.CharField(max_length=120, allow_blank=True)
    start_min = serializers.IntegerField(min_value=0, max_value=1439)
    end_min = serializers.IntegerField(min_value=0, max_value=1439)
    enabled = serializers.BooleanField(required=False, default=True)
    # Días en que se sirve (bit 0 = lunes). Sin declararlo, DRF lo descartaba del payload
    # y cada sincronización del terminal reponía "todos los días" en el servidor.
    days_mask = serializers.IntegerField(
        min_value=0, max_value=127, required=False, allow_null=True,
    )
    all_companies = serializers.BooleanField(required=False, default=True)
    allow_visitors = serializers.BooleanField(required=False, default=True)
    # Turno de almuerzo (para «solo almuerzo»). Un terminal antiguo no lo manda: null → por nombre.
    is_lunch = serializers.BooleanField(required=False, allow_null=True, default=None)
    # Turno de ingreso manual (sin marcaciones). Un terminal antiguo no lo manda: turno normal.
    manual_entry = serializers.BooleanField(required=False, allow_null=True, default=None)
    companies = serializers.ListField(
        child=serializers.CharField(max_length=200, allow_blank=True),
        required=False, default=list,
    )


class VisitorCardConfigSerializer(serializers.Serializer):
    card_no = serializers.CharField(max_length=100)
    label = serializers.CharField(max_length=120, allow_blank=True, required=False, default='')
    enabled = serializers.BooleanField(required=False, default=True)
    created_at = serializers.CharField(max_length=40, allow_blank=True, required=False, default='')


class ConfigSyncSerializer(serializers.Serializer):
    """Configuración compartida (turnos/empresas/tarjetas) con su marca de edición."""

    updated_at = serializers.CharField(max_length=40, allow_blank=True, required=False, default='')
    schedules = ScheduleConfigSerializer(many=True, required=False, default=list)
    visitor_cards = VisitorCardConfigSerializer(many=True, required=False, default=list)
    # Prórroga de cierre de turno. Un terminal antiguo no la manda: se conserva la del servidor.
    shift_overtime_minutes = serializers.IntegerField(
        min_value=1, max_value=180, required=False, allow_null=True,
    )
    # Modo de pruebas (el terminal ignora las marcaciones). Un terminal antiguo no lo manda.
    test_mode = serializers.BooleanField(required=False, allow_null=True, default=None)
    # Inicio y cierre automático de turnos por horario. Un terminal antiguo no lo manda.
    auto_shifts = serializers.BooleanField(required=False, allow_null=True, default=None)


class CardGrantUsedSerializer(serializers.Serializer):
    """Carga de tarjeta de visita que el terminal ya gastó (entregó la colación)."""

    uid = serializers.CharField(max_length=40)
    used_at = serializers.DateTimeField(input_formats=DATETIME_INPUT_FORMATS)
    shift_name = serializers.CharField(max_length=120, allow_blank=True, required=False, default='')


class CardGrantLocalSerializer(serializers.Serializer):
    """Carga de respaldo creada en el terminal (cuando no había conexión con el backoffice)."""

    uid = serializers.CharField(max_length=40)
    card_no = serializers.CharField(max_length=100)
    meal_date = serializers.DateField()
    created_at = serializers.DateTimeField(
        input_formats=DATETIME_INPUT_FORMATS, required=False, allow_null=True, default=None,
    )


class IncidentSerializer(serializers.Serializer):
    """
    Hecho que el terminal informa para la bitácora: por ahora `restart` (la app arrancó;
    `last_alive_at` es la última hora en que estuvo viva antes de eso, si la sabe). El `uid`
    lo hace idempotente: reenviarlo no lo duplica.
    """

    kind = serializers.CharField(max_length=30)
    uid = serializers.CharField(max_length=40)
    at = serializers.DateTimeField(input_formats=DATETIME_INPUT_FORMATS)
    last_alive_at = serializers.DateTimeField(
        input_formats=DATETIME_INPUT_FORMATS, required=False, allow_null=True, default=None,
    )
    detail = serializers.CharField(max_length=300, allow_blank=True, required=False, default='')


class SyncSerializer(serializers.Serializer):
    """Payload de sincronización que sube la estación (todo es opcional)."""

    # Hechos para la bitácora (reinicios del terminal). Un terminal antiguo no los manda.
    incidents = IncidentSerializer(many=True, required=False, default=list)

    # IP del terminal en su red local (la de la interfaz con la que llega al servidor).
    # Un terminal antiguo no la manda: se usa la dirección de origen de la petición.
    local_ip = serializers.IPAddressField(required=False, allow_blank=True, allow_null=True, default='')
    persons = PersonSyncSerializer(many=True, required=False, default=list)
    # true = `persons` es la lista COMPLETA del terminal: lo que no venga se elimina aquí.
    # Un terminal antiguo no lo manda y nunca borra nada.
    persons_complete = serializers.BooleanField(required=False, default=False)
    # Cuándo leyó el terminal la lista de HikCentral, si lo hizo desde su último respaldo.
    persons_refreshed_at = serializers.DateTimeField(
        input_formats=DATETIME_INPUT_FORMATS, required=False, allow_null=True, default=None,
    )
    # true = esa lectura se hizo por la orden «sync_persons» del backoffice: la da por atendida.
    # (No se compara por hora: el reloj del terminal puede no coincidir con el del servidor.)
    persons_refresh_ack = serializers.BooleanField(required=False, default=False)
    # Tarjetas de visita de un solo uso: cargas hechas en el terminal y cargas ya gastadas.
    card_grants_local = CardGrantLocalSerializer(many=True, required=False, default=list)
    card_grants_used = CardGrantUsedSerializer(many=True, required=False, default=list)
    shifts = ShiftSyncSerializer(many=True, required=False, default=list)
    events = EventSyncSerializer(many=True, required=False, default=list)
    config = ConfigSyncSerializer(required=False, allow_null=True)
