import uuid

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import PermissionsMixin
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
from django.utils import timezone
from rest_framework_api_key.models import AbstractAPIKey

from backend.apps.core.managers import UserManager


def new_uid():
    """Identidad estable en el mismo formato que genera el terminal (hex de 32 caracteres)."""
    return uuid.uuid4().hex


# =====================================================================
#  Usuarios del backoffice
# =====================================================================
class User(AbstractBaseUser, PermissionsMixin):
    """
    Usuario del backoffice, autenticado por email.

    El rol decide a qué secciones entra. Lo que no está permitido se niega: no hay
    acceso implícito por estar autenticado (ver `backend.apps.webapp.permissions`).
    """

    class Role(models.TextChoices):
        ADMIN = 'admin', 'Administrador del sistema'
        MANAGER = 'gerente', 'Gerente de administración'
        CASINO = 'casino', 'Personal del casino'

    email = models.EmailField('correo', unique=True)
    first_name = models.CharField('nombre', max_length=150, blank=True)
    last_name = models.CharField('apellido', max_length=150, blank=True)
    # el rol más restringido es el de partida: dar acceso es una decisión explícita
    role = models.CharField('rol', max_length=10, choices=Role.choices, default=Role.CASINO)

    is_active = models.BooleanField('activo', default=True)
    is_staff = models.BooleanField('acceso al admin', default=False)

    date_joined = models.DateTimeField('fecha de creación', default=timezone.now)
    last_login = models.DateTimeField('último ingreso', blank=True, null=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['first_name', 'last_name']

    objects = UserManager()

    class Meta:
        db_table = 'tb_user'
        verbose_name = 'usuario'
        verbose_name_plural = 'usuarios'
        ordering = ['email']

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        # El rol manda sobre el acceso al panel /admin de Django: si se centralizara solo
        # en el formulario, cambiar el rol por otra vía (shell, admin, importación) dejaría
        # a un ex-administrador con la puerta abierta.
        self.is_staff = self.is_superuser or self.role == self.Role.ADMIN
        # Un save() parcial que toque el rol debe escribir is_staff también, o el valor
        # recalculado se quedaría en memoria y el ex-administrador conservaría el acceso.
        update_fields = kwargs.get('update_fields')
        if update_fields is not None:
            update_fields = set(update_fields)
            if update_fields & {'role', 'is_superuser'}:
                kwargs['update_fields'] = update_fields | {'is_staff'}
        return super().save(*args, **kwargs)

    # El administrador del sistema tiene acceso a todo, también dentro del panel /admin de
    # Django: sin esto entraría (is_staff) pero vería un índice vacío, porque no se le
    # asignan permisos por modelo.
    def has_perm(self, perm, obj=None):
        return True if self.is_admin else super().has_perm(perm, obj)

    def has_module_perms(self, app_label):
        return True if self.is_admin else super().has_module_perms(app_label)

    @property
    def full_name(self):
        name = f'{self.first_name} {self.last_name}'.strip()
        return name or self.email

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.first_name or self.email

    # ---- Permisos por rol ----
    # Un superusuario siempre pasa (cuenta de rescate creada por consola).
    @property
    def is_admin(self):
        """Administrador del sistema: acceso a todo, incluida la configuración."""
        return self.is_superuser or self.role == self.Role.ADMIN

    @property
    def can_see_tickets(self):
        """
        Colaciones emitidas: monitor en vivo, control de visitas y reportería.
        Es lo que comparten el gerente de administración y el personal del casino.
        """
        return self.is_admin or self.role in (self.Role.MANAGER, self.Role.CASINO)

    @property
    def can_see_dashboard(self):
        """El panel resume la operación completa: no es para el personal del casino."""
        return self.is_admin or self.role == self.Role.MANAGER

    @property
    def home_url_name(self):
        """
        Sección de entrada tras iniciar sesión, según lo que el rol puede ver.
        None si el rol no da acceso a ninguna sección: en ese caso no hay a dónde
        redirigir y se responde 403 (redirigir provocaría un bucle).
        """
        if self.can_see_dashboard:
            return 'webapp:dashboard'
        if self.can_see_tickets:
            return 'webapp:monitor'
        return None


# =====================================================================
#  Estación (terminal CasinoAccess) y su API key
# =====================================================================
class Station(models.Model):
    """Un terminal CasinoAccess que sube sus registros para respaldo."""

    name = models.CharField('nombre', max_length=120, unique=True)
    device_id = models.PositiveIntegerField(
        'ID de dispositivo', unique=True,
        help_text='Código numérico que se ingresa en el terminal para enrolarlo (ej. 1000).',
    )
    # Hash de la contraseña de enrolado (misma máquina de hashes que los usuarios).
    # Vacía = la estación no acepta enrolarse.
    enroll_password = models.CharField('contraseña de enrolado', max_length=128, blank=True, default='')
    location = models.CharField('ubicación', max_length=200, blank=True)
    is_active = models.BooleanField('activa', default=True)
    created_at = models.DateTimeField('creada', auto_now_add=True)
    last_sync_at = models.DateTimeField('última sincronización', blank=True, null=True)
    # Marca de tiempo de la última edición de la configuración compartida (turnos
    # programados + empresas autorizadas + tarjetas de visita). La misma configuración se
    # edita en la app del terminal y aquí: en cada sync gana la marca más reciente.
    config_updated_at = models.DateTimeField('configuración actualizada', blank=True, null=True)
    # Minutos que un turno sigue abierto después de su hora de término cuando NO hay otro
    # turno a continuación. Treinta segundos antes del cierre el terminal pregunta si se
    # extiende. Parte de la configuración compartida (se edita aquí o en el terminal).
    shift_overtime_minutes = models.PositiveSmallIntegerField(
        'prórroga de turno (minutos)', default=10,
        help_text='Si empieza el turno siguiente, el turno en curso se cierra de inmediato '
                  'sin esperar esta prórroga.',
    )

    class Meta:
        db_table = 'tb_station'
        verbose_name = 'estación'
        verbose_name_plural = 'estaciones'
        ordering = ['name']

    def __str__(self):
        return self.name

    def touch_sync(self):
        self.last_sync_at = timezone.now()
        self.save(update_fields=['last_sync_at'])

    def set_enroll_password(self, raw_password):
        self.enroll_password = make_password(raw_password)

    def check_enroll_password(self, raw_password):
        return bool(self.enroll_password) and check_password(raw_password, self.enroll_password)

    @classmethod
    def next_device_id(cls):
        """Siguiente ID de dispositivo libre, partiendo en 1000."""
        top = cls.objects.aggregate(models.Max('device_id'))['device_id__max']
        return max((top or 0) + 1, 1000)

    def touch_config(self):
        """La configuración compartida se editó en el backoffice: pasa a ser la más reciente."""
        now = timezone.now()
        # precisión de milisegundos: es la que viaja en la sincronización con el terminal
        self.config_updated_at = now.replace(microsecond=(now.microsecond // 1000) * 1000)
        self.save(update_fields=['config_updated_at'])

    def known_companies(self):
        """Empresas distintas en la ficha de personas de la estación ('' = sin empresa)."""
        rows = (self.persons.exclude(user_type='visitor')
                .values_list('company', flat=True).distinct())
        seen, out = set(), []
        for c in rows:
            key = (c or '').strip().upper()
            if key in seen:
                continue
            seen.add(key)
            out.append((c or '').strip())
        out.sort(key=lambda c: (c == '', c.upper()))
        return out


class StationAPIKey(AbstractAPIKey):
    """API key asociada a una estación (patrón de rest_framework_api_key)."""

    station = models.ForeignKey(
        Station,
        on_delete=models.CASCADE,
        related_name='api_keys',
        verbose_name='estación',
    )

    class Meta(AbstractAPIKey.Meta):
        db_table = 'tb_station_api_key'
        verbose_name = 'API key de estación'
        verbose_name_plural = 'API keys de estaciones'


# =====================================================================
#  Datos de dominio respaldados desde CasinoAccess
# =====================================================================
class Person(models.Model):
    """Persona sincronizada desde la estación (tabla persons del SQLite local)."""

    class MealPolicy(models.IntegerChoices):
        """Colación asignada en HikCentral (campo personalizado «Colacion»)."""
        NONE = 0, 'Sin colación'
        LUNCH_ONLY = 1, 'Solo almuerzo'
        ALL_SHIFTS = 2, 'Todos los turnos'

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='persons')
    employee_no = models.CharField('nº empleado', max_length=100)
    name = models.CharField('nombre', max_length=200, blank=True)
    user_type = models.CharField('tipo', max_length=20, default='normal')  # normal | visitor
    company = models.CharField('empresa', max_length=200, blank=True)
    authorized = models.BooleanField('autorizado', default=True)
    person_id = models.CharField('personId HikCentral', max_length=100, blank=True)
    # Colación asignada: la administra HikCentral (el terminal la lee al sincronizar personas
    # y la sube aquí). NULL = el campo está vacío en HikCentral → el terminal la atiende
    # como «todos los turnos» para no cortar el servicio.
    meal_policy = models.SmallIntegerField(
        'colación asignada', blank=True, null=True, choices=MealPolicy.choices,
        help_text='Campo personalizado «Colacion» de HikCentral. Vacío = sin definir '
                  '(el terminal la atiende como «todos los turnos»).',
    )
    updated_at = models.DateTimeField('actualizado', auto_now=True)

    class Meta:
        db_table = 'tb_person'
        verbose_name = 'persona'
        verbose_name_plural = 'personas'
        constraints = [
            models.UniqueConstraint(
                fields=['station', 'employee_no'], name='uq_person_station_employee',
            ),
        ]
        indexes = [models.Index(fields=['station', 'employee_no'])]
        ordering = ['name']

    def __str__(self):
        return f'{self.name or self.employee_no} ({self.employee_no})'

    @property
    def is_visitor(self):
        return self.user_type == 'visitor'

    @property
    def meal_policy_text(self):
        """«Sin colación» / «Solo almuerzo» / «Todos los turnos» / «Sin definir»."""
        if self.meal_policy is None:
            return 'Sin definir'
        return self.MealPolicy(self.meal_policy).label


class Shift(models.Model):
    """
    Turno de colación: UNA apertura concreta en la estación (no la definición horaria,
    que es ShiftSchedule). Cada apertura tiene identidad propia (uid), sabe de qué turno
    programado nació (schedule_uid), a qué día de servicio pertenece y por qué terminó.
    Así se distingue un turno abierto dos veces, uno interrumpido y una reapertura.
    """

    class EndReason(models.TextChoices):
        MANUAL = 'manual', 'Cerrado por el operador'
        REPLACED = 'reemplazado', 'Reemplazado por el turno siguiente'
        EXPIRED = 'expirado', 'Cerrado al vencer la prórroga'
        INTERRUPTED = 'interrumpido', 'Interrumpido (terminal caído)'

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='shifts')
    # Identidad global de la apertura, generada por la estación. Es la clave de ingesta:
    # sobrevive a que se recree la base local del terminal (los remote_id se reinician).
    uid = models.CharField('uid', max_length=40, blank=True, default='')
    remote_id = models.BigIntegerField('id en estación')
    name = models.CharField('nombre', max_length=120)
    started_at = models.DateTimeField('inicio')
    ended_at = models.DateTimeField('término', blank=True, null=True)
    auto = models.BooleanField('automático', default=False)

    # Turno programado del que nació: liga la apertura a su definición aunque se renombre.
    schedule_uid = models.CharField('uid del turno programado', max_length=40, blank=True, default='')
    # Día operacional (un turno que cruza medianoche pertenece al día en que empezó).
    service_date = models.DateField('día de servicio', blank=True, null=True)
    end_reason = models.CharField(
        'motivo de término', max_length=20, choices=EndReason.choices, blank=True, default='',
    )
    # Apertura anterior cuando esta es una reapertura (quedó un comensal fuera).
    reopened_from_uid = models.CharField('reapertura de', max_length=40, blank=True, default='')

    class Meta:
        db_table = 'tb_shift'
        verbose_name = 'turno'
        verbose_name_plural = 'turnos'
        constraints = [
            # El remote_id solo identifica a los turnos respaldados ANTES de que
            # existieran los uid: al recrearse la base del terminal los remote_id se
            # reinician, así que para el resto la identidad es el uid.
            models.UniqueConstraint(
                fields=['station', 'remote_id'], name='uq_shift_station_remote',
                condition=models.Q(uid=''),
            ),
            models.UniqueConstraint(
                fields=['station', 'uid'], name='uq_shift_station_uid',
                condition=models.Q(uid__gt=''),
            ),
        ]
        indexes = [
            models.Index(fields=['station', 'started_at']),
            models.Index(fields=['station', 'schedule_uid', 'service_date']),
        ]
        ordering = ['-started_at']

    def __str__(self):
        return f'{self.name} · {self.started_at:%d-%m-%Y %H:%M}'

    @property
    def is_reopening(self):
        return bool(self.reopened_from_uid)

    @property
    def duration_text(self):
        end = self.ended_at or timezone.now()
        minutes = int((end - self.started_at).total_seconds() // 60)
        h, m = divmod(max(0, minutes), 60)
        return f'{h} h {m} min' if h else f'{m} min'


class AccessEvent(models.Model):
    """Marcación de colación (tabla events del SQLite local)."""

    class Status(models.TextChoices):
        OK = 'Ok', 'Colación válida'
        DUPLICADO = 'Duplicado', 'Duplicado en turno'
        SIN_TURNO = 'SinTurno', 'Fuera de turno'
        NO_AUTORIZADO = 'NoAutorizado', 'No autorizado'

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='events')
    # Identidad global de la marcación (ver Shift.uid): clave de ingesta estable.
    uid = models.CharField('uid', max_length=40, blank=True, default='')
    remote_id = models.BigIntegerField('id en estación')
    shift = models.ForeignKey(
        Shift, on_delete=models.SET_NULL, related_name='events', blank=True, null=True,
    )
    shift_remote_id = models.BigIntegerField('id turno en estación', blank=True, null=True)

    employee_no = models.CharField('nº empleado', max_length=100)
    person_name = models.CharField('nombre', max_length=200, blank=True)
    company = models.CharField('empresa', max_length=200, blank=True)
    verify_method = models.CharField('método verificación', max_length=50, blank=True)
    card_no = models.CharField('nº tarjeta', max_length=100, blank=True)
    event_time = models.DateTimeField('fecha/hora')
    status = models.CharField('estado', max_length=20, choices=Status.choices)
    # Marcación de una VISITA (tarjeta RFID del set de visitas o visita del terminal).
    is_visitor = models.BooleanField('visita', default=False)
    # Motivo del estado (ej. "Empresa X no autorizada en el turno Almuerzo").
    detail = models.CharField('detalle', max_length=300, blank=True, default='')
    # Foto tomada al marcar. Solo se respalda en las marcaciones de VISITA: son pagos
    # adicionales y la foto es la constancia de quién retiró la colación.
    photo = models.BinaryField('foto', blank=True, null=True, editable=False)

    created_at = models.DateTimeField('recibido', auto_now_add=True)

    class Meta:
        db_table = 'tb_access_event'
        verbose_name = 'marcación'
        verbose_name_plural = 'marcaciones'
        constraints = [
            # Igual que en Shift: el remote_id solo identifica lo respaldado antes de los uid.
            models.UniqueConstraint(
                fields=['station', 'remote_id'], name='uq_event_station_remote',
                condition=models.Q(uid=''),
            ),
            models.UniqueConstraint(
                fields=['station', 'uid'], name='uq_event_station_uid',
                condition=models.Q(uid__gt=''),
            ),
        ]
        indexes = [
            models.Index(fields=['station', 'event_time']),
            models.Index(fields=['event_time']),
            models.Index(fields=['status']),
            models.Index(fields=['is_visitor', 'event_time']),
        ]
        ordering = ['-event_time']

    def __str__(self):
        return f'{self.person_name or self.employee_no} · {self.event_time:%d-%m %H:%M} · {self.status}'

    @property
    def has_photo(self):
        return bool(self.photo)


# =====================================================================
#  Configuración compartida con el terminal: turnos programados con sus
#  empresas autorizadas, y set de tarjetas RFID de visitas
# =====================================================================
# Días de la semana en el orden de datetime.weekday(): 0 = lunes … 6 = domingo.
DAY_NAMES_SHORT = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']
DAY_NAMES_LONG = ['lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo']
ALL_DAYS_MASK = 0b1111111   # 127


class ShiftSchedule(models.Model):
    """
    Turno de colación programado (horario) de una estación, con las reglas de acceso:
    qué empresas pueden retirar colación en él (o todas) y si admite visitas.
    Quien no cumpla queda NO AUTORIZADO al marcar en el terminal.
    """

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='schedules')
    # Identidad estable del turno programado, compartida con el terminal: cada apertura
    # queda ligada a ella (Shift.schedule_uid) y sobrevive a renombres y a que la
    # configuración se reemplace entera en cada sincronización.
    uid = models.CharField('uid', max_length=40, default=new_uid)
    name = models.CharField('nombre', max_length=120)
    start_min = models.PositiveSmallIntegerField('inicio (min desde 00:00)')
    end_min = models.PositiveSmallIntegerField('término (min desde 00:00)')
    enabled = models.BooleanField('habilitado', default=True)
    all_companies = models.BooleanField(
        'todas las empresas', default=True,
        help_text='Apagado = solo las empresas autorizadas listadas; el resto queda NO AUTORIZADO.',
    )
    allow_visitors = models.BooleanField('admite visitas (tarjeta RFID)', default=True)
    # Turno de almuerzo: donde pueden retirar las personas con colación «solo almuerzo»
    # (campo «Colacion» = 1 en HikCentral). Las de «sin colación» (0) no retiran en ninguno.
    is_lunch = models.BooleanField(
        'es el turno de almuerzo', default=False,
        help_text='Las personas con colación «solo almuerzo» en HikCentral solo pueden '
                  'retirar en los turnos marcados como almuerzo.',
    )
    # Días en que se sirve el turno, como máscara de bits: bit 0 = lunes … bit 6 = domingo
    # (mismo orden que datetime.weekday()). 127 = todos los días.
    days_mask = models.PositiveSmallIntegerField('días de la semana', default=ALL_DAYS_MASK)
    order = models.PositiveIntegerField('orden', default=0)

    class Meta:
        db_table = 'tb_shift_schedule'
        verbose_name = 'turno programado'
        verbose_name_plural = 'turnos programados'
        ordering = ['station', 'start_min', 'id']

    def __str__(self):
        return f'{self.name} ({self.time_range})'

    @staticmethod
    def looks_like_lunch(name) -> bool:
        """¿El nombre suena a almuerzo? Se usa cuando un terminal antiguo no manda is_lunch."""
        return 'almuerzo' in (name or '').lower()

    @staticmethod
    def fmt_min(m):
        return f'{int(m) // 60:02d}:{int(m) % 60:02d}'

    @property
    def start_text(self):
        return self.fmt_min(self.start_min)

    @property
    def end_text(self):
        return self.fmt_min(self.end_min)

    @property
    def time_range(self):
        return f'{self.start_text} – {self.end_text}'

    def applies_on(self, day) -> bool:
        """¿El turno se sirve ese día? `day` es un date/datetime."""
        return bool(self.days_mask & (1 << day.weekday()))

    @property
    def day_flags(self):
        """[(índice, nombre corto, activo)] para pintar las casillas de días."""
        return [(i, DAY_NAMES_SHORT[i], bool(self.days_mask & (1 << i))) for i in range(7)]

    @property
    def days_text(self):
        """Resumen legible: «Todos los días», «Lun a Vie», «Lun, Mié, Vie», «Ningún día»."""
        days = [i for i in range(7) if self.days_mask & (1 << i)]
        if len(days) == 7:
            return 'Todos los días'
        if not days:
            return 'Ningún día'
        if days == [0, 1, 2, 3, 4]:
            return 'Lun a Vie'
        if days == [5, 6]:
            return 'Fin de semana'
        return ', '.join(DAY_NAMES_SHORT[i] for i in days)

    @property
    def company_names(self):
        return [c.company for c in self.companies.all()]

    @property
    def companies_summary(self):
        if self.all_companies:
            return 'Todas las empresas'
        n = self.companies.count()
        if n == 0:
            return 'Ninguna empresa autorizada'
        return '1 empresa autorizada' if n == 1 else f'{n} empresas autorizadas'


class ShiftScheduleCompany(models.Model):
    """Empresa autorizada en un turno programado ('' = personas sin empresa)."""

    schedule = models.ForeignKey(ShiftSchedule, on_delete=models.CASCADE, related_name='companies')
    company = models.CharField('empresa', max_length=200, blank=True)

    class Meta:
        db_table = 'tb_shift_schedule_company'
        verbose_name = 'empresa autorizada'
        verbose_name_plural = 'empresas autorizadas'
        constraints = [
            models.UniqueConstraint(fields=['schedule', 'company'], name='uq_schedule_company'),
        ]
        ordering = ['company']

    def __str__(self):
        return self.company or '(sin empresa)'


class VisitorCard(models.Model):
    """
    Tarjeta RFID del set de visitas de una estación. Cada tarjeta es una identidad de
    visita: retira UNA colación por turno; una tarjeta ajena al set queda NO AUTORIZADA.
    """

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='visitor_cards')
    card_no = models.CharField('nº tarjeta', max_length=100)
    label = models.CharField('etiqueta', max_length=120, blank=True)
    enabled = models.BooleanField('habilitada', default=True)
    created_at = models.DateTimeField('creada', default=timezone.now)

    class Meta:
        db_table = 'tb_visitor_card'
        verbose_name = 'tarjeta de visita'
        verbose_name_plural = 'tarjetas de visita'
        constraints = [
            models.UniqueConstraint(fields=['station', 'card_no'], name='uq_visitor_card_station'),
        ]
        ordering = ['created_at', 'card_no']

    def __str__(self):
        return self.label or f'Tarjeta {self.card_no}'

    @staticmethod
    def normalize(raw):
        """Misma normalización que la app: sin espacios/controles, en mayúsculas."""
        return ''.join(ch for ch in (raw or '') if not ch.isspace()).upper()

    @property
    def display_name(self):
        return self.label or f'Visita · Tarjeta {self.card_no}'
