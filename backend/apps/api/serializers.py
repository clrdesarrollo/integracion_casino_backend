import base64
import binascii

from rest_framework import serializers

from backend.apps.core.models import AccessEvent

# La estación (CasinoAccess) guarda las horas en hora local con este formato.
DATETIME_INPUT_FORMATS = ['iso-8601', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']


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
    status = serializers.ChoiceField(choices=AccessEvent.Status.values)
    is_visitor = serializers.BooleanField(required=False, default=False)
    detail = serializers.CharField(max_length=300, allow_blank=True, required=False, default='')
    # Foto en base64: solo llega en marcaciones de visita (constancia del pago adicional).
    photo_b64 = serializers.CharField(required=False, allow_blank=True, allow_null=True, default='')

    def validate_photo_b64(self, value):
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


class SyncSerializer(serializers.Serializer):
    """Payload de sincronización que sube la estación (todo es opcional)."""

    persons = PersonSyncSerializer(many=True, required=False, default=list)
    shifts = ShiftSyncSerializer(many=True, required=False, default=list)
    events = EventSyncSerializer(many=True, required=False, default=list)
    config = ConfigSyncSerializer(required=False, allow_null=True)
