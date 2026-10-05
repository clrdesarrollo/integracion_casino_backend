"""
Envío del informe de colaciones por correo.

- `MailSettings`: el servidor SMTP con el que sale el correo (una sola fila, se edita en
  Informes → Servidor de correo). La contraseña se guarda cifrada.
- `ScheduledReport`: un envío periódico — cuándo (todos los días, ciertos días de la
  semana o un día del mes, a una hora), qué período cubre el informe, con qué filtros y a
  quién se manda.
- `ReportDelivery`: cada envío concreto (programado o manual), con el período que cubrió,
  a quién se mandó y si salió. Es a la vez la cola del programador y el historial.

El programador (`manage.py send_scheduled_reports --loop`, ver `mailing.py`) es el que
encola y despacha; la web solo configura y permite enviar a mano.
"""
import base64
import hashlib
import re
from datetime import datetime, time, timedelta

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

from backend.apps.core.models import DAY_NAMES_LONG, Station
from backend.apps.reports.service import last_day_of_month


# =====================================================================
#  Servidor de correo (SMTP)
# =====================================================================
def _fernet():
    """Cifrado simétrico de la contraseña SMTP con una clave derivada de SECRET_KEY."""
    from cryptography.fernet import Fernet

    digest = hashlib.sha256(f'casino-smtp-password:{settings.SECRET_KEY}'.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


class MailSettings(models.Model):
    """
    Servidor SMTP del sistema. Hay una sola fila (pk=1): usar `MailSettings.load()`.

    La contraseña se guarda cifrada con una clave derivada de `DJANGO_SECRET_KEY`, para que
    un respaldo de la base no la deje a la vista. Si esa clave cambia, la contraseña guardada
    deja de poder leerse y hay que volver a ingresarla (`password_unreadable`).
    """

    class Security(models.TextChoices):
        STARTTLS = 'starttls', 'STARTTLS (normalmente puerto 587)'
        SSL = 'ssl', 'SSL/TLS (normalmente puerto 465)'
        NONE = 'none', 'Sin cifrado (normalmente puerto 25)'

    host = models.CharField('servidor SMTP', max_length=200, blank=True)
    port = models.PositiveIntegerField('puerto', default=587,
                                       validators=[MinValueValidator(1), MaxValueValidator(65535)])
    security = models.CharField('cifrado', max_length=10, choices=Security.choices,
                                default=Security.STARTTLS)
    username = models.CharField('usuario', max_length=200, blank=True)
    password_encrypted = models.TextField('contraseña (cifrada)', blank=True, default='')
    from_email = models.EmailField('correo del remitente', blank=True)
    from_name = models.CharField('nombre del remitente', max_length=120, blank=True,
                                 default='Casino Backoffice')

    updated_at = models.DateTimeField('actualizado', auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name='actualizado por',
                                   null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    # Última vez que el programador de envíos dio señales de vida (lo escribe en cada ciclo).
    # Sirve para avisar en pantalla si el servicio no está corriendo.
    scheduler_seen_at = models.DateTimeField('programador visto', null=True, blank=True)

    class Meta:
        db_table = 'tb_mail_settings'
        verbose_name = 'servidor de correo'
        verbose_name_plural = 'servidor de correo'

    def __str__(self):
        return f'{self.host}:{self.port}' if self.host else 'Servidor de correo sin configurar'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def touch_scheduler(cls, now=None):
        cls.load()
        cls.objects.filter(pk=1).update(scheduler_seen_at=now or timezone.now())

    # ---- contraseña ----
    def set_password(self, raw):
        self.password_encrypted = _fernet().encrypt(raw.encode()).decode() if raw else ''

    def _decrypt(self):
        from cryptography.fernet import InvalidToken

        if not self.password_encrypted:
            return ''
        try:
            return _fernet().decrypt(self.password_encrypted.encode()).decode()
        except InvalidToken:
            return None

    @property
    def password(self):
        return self._decrypt() or ''

    @property
    def has_password(self):
        return bool(self.password_encrypted)

    @property
    def password_unreadable(self):
        """Hay una contraseña guardada pero ya no se puede descifrar (cambió SECRET_KEY)."""
        return self.has_password and self._decrypt() is None

    # ---- estado ----
    @property
    def is_configured(self):
        return bool(self.host and self.from_email)

    @property
    def from_header(self):
        from email.utils import formataddr
        return formataddr((self.from_name, self.from_email)) if self.from_name else self.from_email

    #: Tras este tiempo sin señales, se considera que el programador no está corriendo.
    SCHEDULER_STALE_AFTER = timedelta(minutes=3)

    @property
    def scheduler_running(self):
        return bool(self.scheduler_seen_at
                    and timezone.now() - self.scheduler_seen_at < self.SCHEDULER_STALE_AFTER)


# =====================================================================
#  Envíos programados
# =====================================================================
_RECIPIENT_SPLIT = re.compile(r'[\s,;]+')


def split_recipients(text):
    """Direcciones separadas por coma, punto y coma, espacios o saltos de línea; sin repetir."""
    out, seen = [], set()
    for addr in _RECIPIENT_SPLIT.split(text or ''):
        addr = addr.strip()
        if addr and addr.lower() not in seen:
            seen.add(addr.lower())
            out.append(addr)
    return out


def _join_es(items):
    """['a', 'b', 'c'] -> 'a, b y c'."""
    items = list(items)
    if len(items) <= 1:
        return ''.join(items)
    return f'{", ".join(items[:-1])} y {items[-1]}'


def period_text(d_from, d_to):
    if d_from == d_to:
        return f'{d_from:%d-%m-%Y}'
    return f'del {d_from:%d-%m-%Y} al {d_to:%d-%m-%Y}'


# Hasta cuántos días hacia adelante/atrás se busca la próxima/anterior fecha de envío.
# Un envío mensual siempre cae a lo más a 31 días del anterior; se deja holgura.
_HORIZON_DAYS = 62


class ScheduledReport(models.Model):
    """Envío periódico del informe de colaciones en PDF a una lista de correos."""

    class Frequency(models.TextChoices):
        DAILY = 'daily', 'Todos los días'
        WEEKLY = 'weekly', 'Semanal (días de la semana)'
        MONTHLY = 'monthly', 'Mensual (un día del mes)'

    class Period(models.TextChoices):
        PREVIOUS_DAY = 'previous_day', 'Día anterior'
        SAME_DAY = 'same_day', 'Mismo día del envío'
        PREVIOUS_WEEK = 'previous_week', 'Semana anterior (lunes a domingo)'
        PREVIOUS_MONTH = 'previous_month', 'Mes anterior completo'
        SINCE_LAST = 'since_last', 'Desde el envío anterior hasta el día antes del envío'
        LAST_DAYS = 'last_days', 'Últimos N días (hasta el día anterior)'

    name = models.CharField('nombre', max_length=120)
    is_active = models.BooleanField('activo', default=True)

    # ---- cuándo ----
    frequency = models.CharField('frecuencia', max_length=10, choices=Frequency.choices,
                                 default=Frequency.WEEKLY)
    # Días de la semana en que se envía (frecuencia semanal), como máscara de bits: bit 0 =
    # lunes … bit 6 = domingo, el mismo orden que datetime.weekday() y ShiftSchedule.
    weekdays_mask = models.PositiveSmallIntegerField('días de la semana', default=1)
    # Día del mes (frecuencia mensual). Si el mes es más corto, se envía su último día.
    month_day = models.PositiveSmallIntegerField(
        'día del mes', default=1, validators=[MinValueValidator(1), MaxValueValidator(31)])
    send_time = models.TimeField('hora de envío', default=time(8, 0))

    # ---- qué ----
    period = models.CharField('período del informe', max_length=20, choices=Period.choices,
                              default=Period.PREVIOUS_WEEK)
    period_days = models.PositiveSmallIntegerField(
        'cantidad de días', default=7, validators=[MinValueValidator(1), MaxValueValidator(366)])
    # PROTECT: borrar la estación no debe convertir en silencio el envío en «todas las
    # estaciones» ni hacerlo desaparecer.
    station = models.ForeignKey(Station, verbose_name='estación', null=True, blank=True,
                                on_delete=models.PROTECT, related_name='scheduled_reports')
    shift_name = models.CharField('turno', max_length=120, blank=True)
    attach_excel = models.BooleanField('adjuntar también en Excel', default=False)

    # ---- a quién ----
    recipients = models.TextField('destinatarios')
    subject = models.CharField('asunto', max_length=200, blank=True)
    message = models.TextField('mensaje', blank=True)

    next_run_at = models.DateTimeField('próximo envío', null=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name='creado por',
                                   null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name='+')
    created_at = models.DateTimeField('creado', auto_now_add=True)
    updated_at = models.DateTimeField('actualizado', auto_now=True)

    class Meta:
        db_table = 'tb_scheduled_report'
        verbose_name = 'envío programado'
        verbose_name_plural = 'envíos programados'
        ordering = ['name', 'id']

    def __str__(self):
        return self.name

    @property
    def recipient_list(self):
        return split_recipients(self.recipients)

    # ---- calendario ----
    @property
    def weekdays(self):
        return [i for i in range(7) if self.weekdays_mask & (1 << i)]

    def runs_on(self, day):
        """¿Toca enviar ese día? `day` es un date."""
        if self.frequency == self.Frequency.DAILY:
            return True
        if self.frequency == self.Frequency.WEEKLY:
            return bool(self.weekdays_mask & (1 << day.weekday()))
        return day.day == min(self.month_day, last_day_of_month(day).day)

    def run_at(self, day):
        """Momento del envío de ese día (aware, hora local)."""
        return timezone.make_aware(datetime.combine(day, self.send_time),
                                   timezone.get_current_timezone())

    def next_run_after(self, moment):
        """Primer envío estrictamente posterior a `moment` (None si no hay ningún día marcado)."""
        day = timezone.localtime(moment).date()
        for i in range(_HORIZON_DAYS):
            d = day + timedelta(days=i)
            if self.runs_on(d):
                at = self.run_at(d)
                if at > moment:
                    return at
        return None

    def previous_run_date(self, day):
        """Fecha del envío anterior a `day` (sin contar `day`)."""
        for i in range(1, _HORIZON_DAYS):
            d = day - timedelta(days=i)
            if self.runs_on(d):
                return d
        return None

    def schedule_next_run(self, now=None):
        """Recalcula el próximo envío desde ahora (al crear, editar o reactivar)."""
        self.next_run_at = self.next_run_after(now or timezone.now()) if self.is_active else None

    def period_for(self, day):
        """Días [desde, hasta] que cubre el informe enviado el día `day`."""
        P = self.Period
        yesterday = day - timedelta(days=1)
        if self.period == P.SAME_DAY:
            return day, day
        if self.period == P.PREVIOUS_WEEK:
            monday = day - timedelta(days=day.weekday() + 7)
            return monday, monday + timedelta(days=6)
        if self.period == P.PREVIOUS_MONTH:
            end = day.replace(day=1) - timedelta(days=1)
            return end.replace(day=1), end
        if self.period == P.LAST_DAYS:
            return yesterday - timedelta(days=max(1, self.period_days) - 1), yesterday
        if self.period == P.SINCE_LAST:
            # p. ej. mensual el 24: del 24 del mes anterior al 23 de este
            return self.previous_run_date(day) or yesterday, yesterday
        return yesterday, yesterday

    # ---- textos para pantalla y correo ----
    @property
    def schedule_text(self):
        hora = f'a las {self.send_time:%H:%M}'
        if self.frequency == self.Frequency.DAILY:
            return f'Todos los días {hora}'
        if self.frequency == self.Frequency.WEEKLY:
            days = self.weekdays
            if len(days) == 7:
                return f'Todos los días {hora}'
            if days == [0, 1, 2, 3, 4]:
                return f'De lunes a viernes {hora}'
            if not days:
                return 'Ningún día'
            return f'Cada {_join_es(DAY_NAMES_LONG[i] for i in days)} {hora}'
        if self.month_day >= 31:
            return f'El último día de cada mes {hora}'
        extra = ' (o el último, si el mes es más corto)' if self.month_day > 28 else ''
        return f'El día {self.month_day} de cada mes{extra} {hora}'

    @property
    def period_label(self):
        if self.period == self.Period.LAST_DAYS:
            return f'Últimos {self.period_days} días (hasta el día anterior)'
        if self.period == self.Period.SINCE_LAST:
            return 'Desde el envío anterior'
        return self.get_period_display()

    @property
    def filters_text(self):
        station = self.station.name if self.station_id else 'Todas las estaciones'
        shift = f'turno {self.shift_name}' if self.shift_name else 'todos los turnos'
        return f'{station} · {shift}'

    def period_preview(self, day=None):
        """Período que cubriría un envío hecho `day` (por defecto hoy), en texto."""
        return period_text(*self.period_for(day or timezone.localdate()))

    @property
    def next_period_text(self):
        if not self.next_run_at:
            return ''
        return self.period_preview(timezone.localtime(self.next_run_at).date())


class ReportDelivery(models.Model):
    """
    Un envío concreto del informe: el período que cubrió, a quién y si salió.

    Los programados nacen «en cola» cuando les llega la hora y el programador los despacha;
    si el servidor de correo falla se reintentan solos (ver `mailing.MAX_ATTEMPTS`). Los
    manuales («Enviar ahora») se intentan una vez, en el momento.
    """

    class Status(models.TextChoices):
        PENDING = 'pending', 'En cola'
        SENT = 'sent', 'Enviado'
        FAILED = 'failed', 'Falló'

    class Trigger(models.TextChoices):
        SCHEDULE = 'schedule', 'Programado'
        MANUAL = 'manual', 'Manual'

    # SET_NULL: el historial se conserva aunque se elimine el envío programado
    report = models.ForeignKey(ScheduledReport, verbose_name='envío programado', null=True,
                               blank=True, on_delete=models.SET_NULL, related_name='deliveries')
    report_name = models.CharField('envío', max_length=120)
    trigger = models.CharField('origen', max_length=10, choices=Trigger.choices,
                               default=Trigger.SCHEDULE)
    # El envío programado al que corresponde (o el momento en que se pidió el manual)
    scheduled_for = models.DateTimeField('programado para')
    date_from = models.DateField('desde')
    date_to = models.DateField('hasta')
    recipients = models.TextField('destinatarios')

    status = models.CharField('estado', max_length=10, choices=Status.choices,
                              default=Status.PENDING)
    attempts = models.PositiveSmallIntegerField('intentos', default=0)
    next_attempt_at = models.DateTimeField('próximo intento', null=True, blank=True)
    sent_at = models.DateTimeField('enviado', null=True, blank=True)
    error = models.TextField('error', blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, verbose_name='pedido por',
                                     null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name='+')
    created_at = models.DateTimeField('creado', auto_now_add=True)

    class Meta:
        db_table = 'tb_report_delivery'
        verbose_name = 'envío de informe'
        verbose_name_plural = 'envíos de informes'
        ordering = ['-scheduled_for', '-id']
        constraints = [
            # cada fecha de un envío programado se encola una sola vez
            models.UniqueConstraint(fields=['report', 'scheduled_for'],
                                    condition=models.Q(trigger='schedule'),
                                    name='uq_delivery_report_occurrence'),
        ]
        indexes = [models.Index(fields=['status', 'next_attempt_at'])]

    def __str__(self):
        return f'{self.report_name} · {self.period_text}'

    @property
    def period_text(self):
        return period_text(self.date_from, self.date_to)

    @property
    def recipient_list(self):
        return split_recipients(self.recipients)
