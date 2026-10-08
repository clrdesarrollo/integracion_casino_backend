import uuid
from datetime import timedelta

from django.contrib.auth.hashers import check_password, make_password
from django.contrib.auth.models import PermissionsMixin
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
from django.utils import timezone
from django.utils.text import slugify
from rest_framework_api_key.models import AbstractAPIKey

from backend.apps.core.access import CAPABILITIES, CAPABILITY_CODES
from backend.apps.core.managers import UserManager


def new_uid():
    """Identidad estable en el mismo formato que genera el terminal (hex de 32 caracteres)."""
    return uuid.uuid4().hex


# =====================================================================
#  Roles del backoffice
# =====================================================================
class Role(models.Model):
    """
    Conjunto de capacidades (ver `core.access`) que se asigna a los usuarios.

    Los roles se crean y editan desde el backoffice. Hay uno especial, el administrador
    (`is_admin`): tiene siempre todas las capacidades y no se puede editar ni borrar, para
    que nadie se quede sin cómo administrar el sistema.
    """

    name = models.CharField('nombre', max_length=80, unique=True)
    # identificador estable: los tres roles originales son admin, gerente y casino
    code = models.SlugField('código', max_length=60, unique=True, editable=False)
    description = models.CharField('descripción', max_length=255, blank=True)
    permissions = models.JSONField('permisos', default=list, blank=True)
    # acceso total: solo el administrador del sistema
    is_admin = models.BooleanField('acceso total', default=False, editable=False)
    # los roles de sistema no se pueden eliminar
    is_system = models.BooleanField('rol de sistema', default=False, editable=False)
    created_at = models.DateTimeField('creado', auto_now_add=True)

    class Meta:
        db_table = 'tb_role'
        verbose_name = 'rol'
        verbose_name_plural = 'roles'
        ordering = ['-is_admin', 'name']

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.is_admin:
            self.permissions = list(CAPABILITY_CODES)
        else:
            # solo códigos conocidos y sin repetir (una capacidad retirada del catálogo
            # no debe quedar como permiso fantasma)
            self.permissions = [c for c in CAPABILITY_CODES if c in set(self.permissions or [])]
        if not self.code:
            self.code = self._unique_code()
        return super().save(*args, **kwargs)

    def _unique_code(self):
        base = slugify(self.name)[:50] or 'rol'
        code, n = base, 2
        while Role.objects.filter(code=code).exists():
            code, n = f'{base}-{n}', n + 1
        return code

    def allows(self, capability):
        return self.is_admin or capability in (self.permissions or [])


# =====================================================================
#  Usuarios del backoffice
# =====================================================================
class User(AbstractBaseUser, PermissionsMixin):
    """
    Usuario del backoffice, autenticado por email.

    El rol decide a qué secciones entra. Lo que no está permitido se niega: no hay
    acceso implícito por estar autenticado (ver `backend.apps.webapp.permissions`).
    """

    email = models.EmailField('correo', unique=True)
    first_name = models.CharField('nombre', max_length=150, blank=True)
    last_name = models.CharField('apellido', max_length=150, blank=True)
    # PROTECT: no se elimina un rol que aún tiene usuarios. Si no se indica, el manager
    # asigna el rol «casino», el más restringido: dar acceso es una decisión explícita.
    role = models.ForeignKey(Role, verbose_name='rol', on_delete=models.PROTECT,
                             related_name='users')

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
        self.is_staff = self.is_superuser or self.role.is_admin
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
        """Administrador del sistema: acceso total, incluido el panel /admin de Django."""
        return self.is_superuser or self.role.is_admin

    def has_cap(self, capability):
        """¿Puede usar esta capacidad del backoffice? (ver `core.access`)"""
        return self.is_superuser or self.role.allows(capability)

    @property
    def caps(self):
        """Capacidades como diccionario, para las plantillas: `{% if user.caps.reports %}`."""
        return {c.code: self.has_cap(c.code) for c in CAPABILITIES}

    @property
    def home_url_name(self):
        """
        Sección de entrada tras iniciar sesión: la primera que el rol puede ver.
        None si el rol no da acceso a ninguna: en ese caso no hay a dónde redirigir y se
        responde 403 (redirigir provocaría un bucle).
        """
        for cap in CAPABILITIES:
            if self.has_cap(cap.code):
                return cap.home
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
    # Dirección IP del terminal en su red local, tal como la informó en su última
    # sincronización (sirve para ubicarlo y conectarse por SSH al darle soporte).
    last_ip = models.CharField('dirección IP', max_length=45, blank=True, default='')
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

    # Inicio y cierre automático de turnos: activado, el terminal abre cada turno al llegar
    # su hora de inicio y lo cierra a su hora de término, sin prórroga ni intervención.
    # Desactivado, el operador inicia el turno y al terminar el horario corre la prórroga.
    # Configuración compartida: se cambia aquí o en el terminal.
    auto_shifts = models.BooleanField('inicio y cierre automático de turnos', default=False)

    # Modo de pruebas: mientras está activo el terminal IGNORA las marcaciones (las muestra
    # en pantalla como prueba, pero no las registra, no las cuenta ni gasta tarjetas). Es
    # parte de la configuración compartida: se activa aquí o en el propio terminal.
    test_mode = models.BooleanField('modo de pruebas', default=False)

    # Personas: cuándo el terminal leyó por última vez la lista de HikCentral, y si desde
    # el backoffice se le pidió volver a leerla (botón «Actualizar desde HikCentral»). La
    # solicitud queda pendiente hasta que el terminal confirma una lectura posterior.
    persons_refreshed_at = models.DateTimeField(
        'personas leídas de HikCentral', blank=True, null=True)
    persons_refresh_requested_at = models.DateTimeField(
        'actualización de personas solicitada', blank=True, null=True)

    class Meta:
        db_table = 'tb_station'
        verbose_name = 'estación'
        verbose_name_plural = 'estaciones'
        ordering = ['name']

    def __str__(self):
        return self.name

    #: Minutos sin sincronizar a partir de los cuales el regreso del terminal se anota en
    #: la bitácora (el terminal sincroniza cada pocos minutos y al instante con cada marcación).
    SYNC_SILENCE_MINUTES = 15

    def touch_sync(self, ip=''):
        from backend.apps.core import audit

        now = timezone.now()
        previous = self.last_sync_at
        self.last_sync_at = now
        fields = ['last_sync_at']
        if ip and ip != self.last_ip:
            self.last_ip = ip
            fields.append('last_ip')
        self.save(update_fields=fields)

        # Un terminal que vuelve después de un rato sin contacto es un hecho que vale la
        # pena reconstruir después (corte de energía, red caída, kiosco apagado).
        if previous is None:
            audit.log('terminal.primera_sync', 'El terminal sincronizó por primera vez.',
                      station=self, ip=ip)
        elif now - previous >= timedelta(minutes=self.SYNC_SILENCE_MINUTES):
            audit.log(
                'terminal.sync_reanudada',
                f'El terminal volvió a sincronizar tras {audit.duration_text(now - previous)} '
                f'sin contacto (última sincronización: {audit.local_text(previous)}).',
                station=self, ip=ip, level=audit.WARNING,
                data={'last_sync_at': previous.isoformat(),
                      'silence_seconds': int((now - previous).total_seconds())},
            )

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
        # el terminal conectado la viene a buscar ahora, sin esperar su próxima sincronización
        from backend.apps.realtime.broadcast import send_station_command
        send_station_command(self.pk, 'sync')

    def notify_card_grants_changed(self):
        """Se cargó, devolvió o eliminó una tarjeta de visita: el terminal la recoge ahora."""
        from backend.apps.realtime.broadcast import send_station_command
        send_station_command(self.pk, 'sync_grants')

    def request_persons_refresh(self):
        """Pide al terminal que vuelva a leer las personas de HikCentral y las suba."""
        self.persons_refresh_requested_at = timezone.now()
        self.save(update_fields=['persons_refresh_requested_at'])
        from backend.apps.realtime.broadcast import send_station_command
        send_station_command(self.pk, 'sync_persons')

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
    # SHA-1 de la foto de perfil respaldada (PersonPhoto); vacío = sin foto. El terminal
    # manda el hash en cada sincronización y solo sube la imagen cuando no coincide.
    photo_hash = models.CharField('hash de la foto', max_length=40, blank=True, default='')
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

    @property
    def photo_url(self):
        """URL de la foto de perfil ('' si no tiene). El hash la versiona para la caché."""
        return self.photo_url_for(self.pk, self.photo_hash) if self.photo_hash else ''

    @staticmethod
    def photo_url_for(pk, photo_hash):
        from django.urls import reverse  # import diferido: los modelos no dependen de las rutas
        return f"{reverse('webapp:person_photo', args=[pk])}?v={photo_hash[:12]}"

    @classmethod
    def attach_photos_to_events(cls, events):
        """
        Pone en cada marcación `person_photo_url`: la foto de la FICHA de quien marcó ('' si
        no tiene o es una tarjeta de visita). Una sola consulta para toda la lista.
        """
        events = list(events)
        for ev in events:
            ev.person_photo_url = ''
        wanted = [ev for ev in events if not ev.is_visitor]
        if not wanted:
            return events
        rows = cls.objects.filter(
            station_id__in={ev.station_id for ev in wanted},
            employee_no__in={ev.employee_no for ev in wanted},
        ).exclude(photo_hash='').values_list('station_id', 'employee_no', 'pk', 'photo_hash')
        urls = {(st, no): cls.photo_url_for(pk, h) for st, no, pk, h in rows}
        for ev in wanted:
            ev.person_photo_url = urls.get((ev.station_id, ev.employee_no), '')
        return events


class PersonPhoto(models.Model):
    """
    Foto de perfil de la persona (la de HikCentral, que el terminal descarga y respalda
    aquí). Vive en su propia tabla para que las consultas de personas no carguen la imagen.
    """

    person = models.OneToOneField(Person, on_delete=models.CASCADE, related_name='photo')
    data = models.BinaryField('foto', editable=False)
    updated_at = models.DateTimeField('actualizado', auto_now=True)

    class Meta:
        db_table = 'tb_person_photo'
        verbose_name = 'foto de persona'
        verbose_name_plural = 'fotos de personas'


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
        SCHEDULED = 'horario', 'Cerrado al terminar su horario'
        INTERRUPTED = 'interrumpido', 'Interrumpido (terminal caído)'
        MANUAL_ENTRY = 'ingreso_manual', 'Ingreso manual de colaciones'

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
    # Turno de ingreso manual (ShiftSchedule.manual_entry): nadie marca y la cocinera registra
    # la cantidad de colaciones preparadas. El registro nace cerrado y sin marcaciones; esta
    # cantidad es lo que se contabiliza. NULL = turno normal, con marcaciones.
    manual_count = models.PositiveIntegerField('colaciones ingreso manual', blank=True, null=True)

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
    def is_manual_entry(self):
        return self.manual_count is not None

    @property
    def start_mode(self):
        """Cómo se abrió: 'auto' (el planificador, por horario) o 'manual' (el operador)."""
        return 'auto' if self.auto else 'manual'

    @property
    def end_mode(self):
        """
        Cómo se cerró: 'manual' (el operador), 'auto' (por horario: lo reemplazó el turno
        siguiente o venció la prórroga) o 'interrumpido' (terminal caído). '' si sigue en
        curso o lo respaldó un terminal antiguo, que no informaba el motivo.
        """
        if not self.ended_at:
            return ''
        return {
            self.EndReason.MANUAL: 'manual',
            self.EndReason.REPLACED: 'auto',
            self.EndReason.EXPIRED: 'auto',
            self.EndReason.SCHEDULED: 'auto',
            self.EndReason.INTERRUPTED: 'interrumpido',
        }.get(self.end_reason, '')

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
        # Solo para ingresos manuales del backoffice que se anularon: la fila se conserva
        # (con quién y por qué) pero deja de contar. El terminal nunca manda este estado.
        ANULADO = 'Anulado', 'Anulado'

    class Origin(models.TextChoices):
        TERMINAL = 'terminal', 'Terminal'
        BACKOFFICE = 'backoffice', 'Ingreso manual (backoffice)'

    #: Estados que el terminal puede informar (el resto son del backoffice).
    TERMINAL_STATUSES = ('Ok', 'Duplicado', 'SinTurno', 'NoAutorizado')

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

    # De dónde salió la marcación. 'backoffice' = la registró a mano un usuario del
    # backoffice porque la colación se sirvió pero no se pudo marcar en el kiosco (corte
    # de energía, terminal caído). Lleva quién la registró y por qué, y queda marcada como
    # manual en todos los informes. No se edita ni se borra: si estuvo mal, se anula
    # (status = Anulado) y la fila sigue ahí con el motivo de la anulación.
    origin = models.CharField('origen', max_length=12, choices=Origin.choices,
                              default=Origin.TERMINAL)
    entered_by = models.ForeignKey(
        'User', on_delete=models.SET_NULL, blank=True, null=True, related_name='manual_events',
        verbose_name='registrada por')
    entered_by_name = models.CharField('registrada por', max_length=200, blank=True, default='')
    entry_reason = models.CharField('motivo del ingreso manual', max_length=300, blank=True, default='')
    annulled_at = models.DateTimeField('anulada el', blank=True, null=True)
    annulled_by = models.ForeignKey(
        'User', on_delete=models.SET_NULL, blank=True, null=True, related_name='annulled_events',
        verbose_name='anulada por')
    annulled_by_name = models.CharField('anulada por', max_length=200, blank=True, default='')
    annul_reason = models.CharField('motivo de la anulación', max_length=300, blank=True, default='')

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

    @property
    def is_manual(self):
        """Ingresada a mano desde el backoffice (no la marcó el terminal)."""
        return self.origin == self.Origin.BACKOFFICE

    @property
    def is_annulled(self):
        return self.status == self.Status.ANULADO

    @property
    def manual_note(self):
        """«Registrada en el backoffice por X el 08-10-2026 10:15 · Motivo: …» ('' si no es manual)."""
        if not self.is_manual:
            return ''
        when = timezone.localtime(self.created_at) if self.created_at else None
        text = f'Registrada en el backoffice por {self.entered_by_name or "—"}'
        if when:
            text += f' el {when:%d-%m-%Y %H:%M}'
        if self.entry_reason:
            text += f' · Motivo: {self.entry_reason}'
        return text

    @property
    def annul_note(self):
        if not self.is_annulled:
            return ''
        when = timezone.localtime(self.annulled_at) if self.annulled_at else None
        text = f'Anulada por {self.annulled_by_name or "—"}'
        if when:
            text += f' el {when:%d-%m-%Y %H:%M}'
        if self.annul_reason:
            text += f' · Motivo: {self.annul_reason}'
        return text


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
    # Turno sin acceso de personas (p. ej. la once, que se deja preparada fuera del casino):
    # en el terminal no se abre, la cocinera registra la cantidad de colaciones.
    manual_entry = models.BooleanField(
        'ingreso manual', default=False,
        help_text='Nadie marca en este turno: en el terminal la cocinera ingresa la cantidad '
                  'de colaciones que deja preparadas. Nunca se abre solo por horario.',
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

# =====================================================================
#  Registro de visitas: a quién se entregó cada tarjeta física, quién la entregó y a
#  quién venía a ver. Es el respaldo administrativo de las colaciones de visita.
# =====================================================================
class VisitQuerySet(models.QuerySet):
    def open(self):
        """Visitas con la tarjeta todavía entregada (sin devolución registrada)."""
        return self.filter(returned_at__isnull=True)


class Visit(models.Model):
    """
    Entrega de una tarjeta de visita a una persona concreta.

    La tarjeta se identifica por su número (no por FK a `VisitorCard`): el set de tarjetas
    se reemplaza completo cuando el terminal sincroniza su configuración, y el registro de
    la visita debe sobrevivir a eso. La etiqueta se guarda como estaba al entregar.

    El funcionario visitado se toma de la ficha de personas de la estación (HikCentral),
    pero se conserva su nombre en texto: la ficha puede desaparecer al resincronizar y la
    visita debe seguir diciendo a quién vino a ver.
    """

    station = models.ForeignKey(Station, on_delete=models.CASCADE, related_name='visits')

    # --- tarjeta entregada ---
    card_no = models.CharField('nº tarjeta', max_length=100)
    card_label = models.CharField('etiqueta de la tarjeta', max_length=120, blank=True)

    # --- la visita ---
    visitor_name = models.CharField('nombre de la visita', max_length=200)
    visitor_document = models.CharField('RUT / documento', max_length=40, blank=True)
    visitor_company = models.CharField('empresa de procedencia', max_length=200, blank=True)

    # --- a quién viene a ver ---
    host_company = models.CharField('empresa visitada', max_length=200, blank=True)
    host_person = models.ForeignKey(
        Person, on_delete=models.SET_NULL, blank=True, null=True, related_name='hosted_visits',
    )
    host_name = models.CharField('funcionario visitado', max_length=200, blank=True)

    # --- quién entregó / recibió la tarjeta ---
    delivered_by = models.ForeignKey(
        'User', on_delete=models.SET_NULL, blank=True, null=True, related_name='visits_delivered',
    )
    delivered_by_name = models.CharField('entregada por', max_length=200, blank=True)
    delivered_at = models.DateTimeField('entregada el', default=timezone.now)
    returned_by = models.ForeignKey(
        'User', on_delete=models.SET_NULL, blank=True, null=True, related_name='visits_returned',
    )
    returned_by_name = models.CharField('recibida por', max_length=200, blank=True)
    returned_at = models.DateTimeField('devuelta el', blank=True, null=True)

    notes = models.CharField('observación', max_length=300, blank=True)
    created_at = models.DateTimeField('creada', auto_now_add=True)

    # --- colación autorizada: la entrega «carga» la tarjeta con UNA colación ---
    # Identidad de la carga, compartida con el terminal (que la marca como usada).
    uid = models.CharField('uid', max_length=40, default=new_uid, db_index=True)
    # Día y turno para los que vale. Sin turno (uid vacío) = cualquier turno de ese día.
    meal_date = models.DateField('día de la colación', blank=True, null=True)
    meal_schedule_uid = models.CharField('uid del turno autorizado', max_length=40, blank=True, default='')
    meal_shift_name = models.CharField('turno autorizado', max_length=120, blank=True, default='')
    # Cuándo el terminal leyó la tarjeta y entregó la colación (la carga queda gastada).
    used_at = models.DateTimeField('utilizada el', blank=True, null=True)
    used_shift_name = models.CharField('turno en que se utilizó', max_length=120, blank=True, default='')
    # 'backoffice' = registrada en el mesón; 'totem' = carga de respaldo hecha en el terminal.
    origin = models.CharField('origen', max_length=20, default='backoffice')

    objects = VisitQuerySet.as_manager()

    class Meta:
        db_table = 'tb_visit'
        verbose_name = 'visita'
        verbose_name_plural = 'visitas'
        ordering = ['-delivered_at']
        indexes = [
            models.Index(fields=['station', 'card_no', 'delivered_at']),
            models.Index(fields=['delivered_at']),
        ]

    def __str__(self):
        return f'{self.visitor_name} · tarjeta {self.card_no} · {self.delivered_at:%d-%m %H:%M}'

    @property
    def is_open(self):
        return self.returned_at is None

    @property
    def card_display(self):
        return self.card_label or f'Tarjeta {self.card_no}'

    @property
    def host_display(self):
        """«Nicolás Muñoz (PTJ)», solo la empresa, solo el nombre, o vacío."""
        name, company = self.host_name.strip(), self.host_company.strip()
        if name and company:
            return f'{name} ({company})'
        return name or company

    @property
    def meal_text(self):
        """«Almuerzo · 02-10-2026», «Cualquier turno · 02-10-2026» o vacío (registro antiguo)."""
        if not self.meal_date:
            return ''
        return f"{self.meal_shift_name or 'Cualquier turno'} · {self.meal_date:%d-%m-%Y}"

    @property
    def meal_state(self):
        """'used', 'pending', 'expired' o '' (registro anterior a las tarjetas de un solo uso)."""
        if self.used_at:
            return 'used'
        if not self.meal_date:
            return ''
        if self.returned_at is not None or self.meal_date < timezone.localdate():
            return 'expired'
        return 'pending'

    @classmethod
    def grants_for(cls, station):
        """
        Cargas vigentes de las tarjetas de la estación, tal como las necesita el terminal:
        las de tarjetas aún entregadas, de hoy en adelante (y ayer, por el turno que cruza
        la medianoche). Las ya usadas también van, para que el terminal diga «ya utilizada».
        """
        since = timezone.localdate() - timedelta(days=1)
        rows = cls.objects.open().filter(station=station, meal_date__gte=since).order_by('delivered_at')
        return [{
            'uid': v.uid,
            'card_no': v.card_no,
            'meal_date': v.meal_date.isoformat(),
            'schedule_uid': v.meal_schedule_uid,
            'shift_name': v.meal_shift_name,
            'visitor_name': v.visitor_name,
            'used': v.used_at is not None,
        } for v in rows]

    @classmethod
    def register_totem_grants(cls, station, items):
        """
        Cargas de respaldo hechas en el terminal (sin conexión con el backoffice): se
        registran como una entrega sin datos de la visita, para que el uso quede a la vista.
        """
        for g in items:
            if cls.objects.filter(station=station, uid=g['uid']).exists():
                continue
            card_no = VisitorCard.normalize(g['card_no'])
            card = station.visitor_cards.filter(card_no=card_no).first()
            when = g.get('created_at') or timezone.now()
            for previous in cls.objects.open().filter(station=station, card_no=card_no):
                previous.close(when=when)
            cls.objects.create(
                station=station, uid=g['uid'], card_no=card_no,
                card_label=card.label if card else '',
                visitor_name='(Carga hecha en el terminal)',
                delivered_by_name='Terminal', delivered_at=when,
                meal_date=g['meal_date'], origin='totem',
                notes='Carga de respaldo desde el terminal, sin registro de la visita.',
            )

    @classmethod
    def mark_used(cls, station, items):
        """El terminal entregó la colación de estas cargas: quedan gastadas (idempotente)."""
        for g in items:
            cls.objects.filter(station=station, uid=g['uid'], used_at__isnull=True).update(
                used_at=g['used_at'], used_shift_name=g.get('shift_name') or '')

    def covers(self, when):
        """La tarjeta estaba en manos de esta visita en el instante `when`."""
        if when < self.delivered_at:
            return False
        return self.returned_at is None or when < self.returned_at

    def close(self, user=None, when=None):
        """Registra la devolución de la tarjeta (idempotente)."""
        if self.returned_at is not None:
            return
        self.returned_at = when or timezone.now()
        self.returned_by = user if (user is not None and user.pk) else None
        self.returned_by_name = user.full_name if user is not None else ''
        self.save(update_fields=['returned_at', 'returned_by', 'returned_by_name'])

    @classmethod
    def attach_to_events(cls, events):
        """
        Pone en cada marcación de visita el registro de visita vigente para su tarjeta
        (`event.visit`, None si la tarjeta se usó sin registrar la entrega).

        Una sola consulta: las visitas de esas tarjetas entregadas antes de la última
        marcación y no devueltas antes de la primera.
        """
        events = [e for e in events if e.is_visitor and e.card_no]
        if not events:
            return
        times = [e.event_time for e in events]
        first, last = min(times), max(times)
        keys = {(e.station_id, e.card_no) for e in events}
        candidates = (cls.objects
                      .filter(station_id__in={k[0] for k in keys},
                              card_no__in={k[1] for k in keys},
                              delivered_at__lte=last)
                      .filter(models.Q(returned_at__isnull=True) | models.Q(returned_at__gt=first))
                      .order_by('delivered_at'))
        by_card = {}
        for v in candidates:
            by_card.setdefault((v.station_id, v.card_no), []).append(v)
        for e in events:
            e.visit = None
            # la más reciente que cubra el instante: si se reentregó sin devolver, manda la nueva
            for v in reversed(by_card.get((e.station_id, e.card_no), [])):
                if v.covers(e.event_time):
                    e.visit = v
                    break


# =====================================================================
#  Bitácora: qué pasó, cuándo, en qué estación y quién lo hizo
# =====================================================================
class AuditLog(models.Model):
    """
    Una línea por hecho (ver `core.audit`). No se edita ni se borra desde el backoffice:
    es el registro con el que se reconstruye un incidente (p. ej. a qué hora se cayó el
    terminal y qué colaciones se ingresaron después a mano).
    """

    class Level(models.TextChoices):
        INFO = 'info', 'Información'
        WARNING = 'warning', 'Atención'
        ERROR = 'error', 'Error'

    # Instante del hecho (no de su registro: un turno que el terminal informa más tarde se
    # anota a su hora real, y la de recepción va en `data`).
    at = models.DateTimeField('fecha y hora', default=timezone.now, db_index=True)
    category = models.CharField('categoría', max_length=20, db_index=True)
    action = models.CharField('acción', max_length=60)
    level = models.CharField('nivel', max_length=10, choices=Level.choices, default=Level.INFO)
    summary = models.CharField('resumen', max_length=400)

    station = models.ForeignKey(Station, on_delete=models.SET_NULL, blank=True, null=True,
                                related_name='audit_entries')
    station_name = models.CharField('estación', max_length=120, blank=True, default='')
    # Quién lo hizo. Vacío = el terminal o el propio sistema.
    user = models.ForeignKey('User', on_delete=models.SET_NULL, blank=True, null=True,
                             related_name='audit_entries')
    user_name = models.CharField('usuario', max_length=200, blank=True, default='')
    ip = models.CharField('IP', max_length=45, blank=True, default='')
    # Enlaces al turno o la marcación a la que se refiere (si siguen existiendo).
    shift = models.ForeignKey(Shift, on_delete=models.SET_NULL, blank=True, null=True,
                              related_name='audit_entries')
    event = models.ForeignKey(AccessEvent, on_delete=models.SET_NULL, blank=True, null=True,
                              related_name='audit_entries')
    # Detalle del hecho (antes/después, datos de la persona, etc.).
    data = models.JSONField('detalle', default=dict, blank=True)

    class Meta:
        db_table = 'tb_audit_log'
        verbose_name = 'entrada de bitácora'
        verbose_name_plural = 'bitácora'
        ordering = ['-at', '-id']
        indexes = [
            models.Index(fields=['category', 'at']),
            models.Index(fields=['station', 'at']),
        ]

    def __str__(self):
        return f'{self.at:%d-%m %H:%M:%S} · {self.action} · {self.summary[:60]}'

    @property
    def category_label(self):
        from backend.apps.core.audit import CATEGORY_LABELS
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def actor(self):
        """Quién: el usuario, o «Terminal» / «Sistema» cuando no hay usuario."""
        if self.user_name:
            return self.user_name
        return 'Terminal' if self.category in ('terminal', 'turno') else 'Sistema'

    @property
    def data_items(self):
        """Pares (clave, valor) del detalle, para mostrarlos sin JSON crudo."""
        return [(k, v) for k, v in (self.data or {}).items()]
