"""
Envío del informe por correo y el programador de envíos.

El programador (`manage.py send_scheduled_reports --loop`, un servicio aparte en
docker-compose) llama a `run_scheduler_tick` cada medio minuto:

1. A cada envío programado al que le llegó la hora le encola un `ReportDelivery` con el
   período que cubre, y le calcula el próximo envío. Si el programador estuvo detenido y
   pasaron varias fechas, se envía solo la más reciente (no una ráfaga de atrasados).
2. Intenta cada envío en cola. Si falla (servidor caído, credenciales, etc.) se reintenta
   a los 10 y a los 30 minutos; tras el tercer intento queda «Falló», con el error a la
   vista en el historial, y se puede reintentar a mano.

El correo sale por el servidor SMTP configurado en el backoffice (`MailSettings`). Se usa
el backend de correo de Django que indique `EMAIL_BACKEND` (SMTP por defecto; en las
pruebas Django lo cambia por el de memoria), con el servidor y credenciales de la base.
"""
import logging
import smtplib
import socket
import ssl
from datetime import timedelta

from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.utils import timezone

from backend.apps.reports.models import (
    MailSettings, ReportDelivery, ScheduledReport, period_text,
)

logger = logging.getLogger(__name__)

SMTP_TIMEOUT = 30            # segundos por operación con el servidor
MAX_ATTEMPTS = 3             # intentos de un envío programado antes de darlo por fallido
RETRY_DELAYS = (timedelta(minutes=10), timedelta(minutes=30))

XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class MailError(Exception):
    """Error de envío con un mensaje entendible para quien usa el backoffice."""


# =====================================================================
#  SMTP
# =====================================================================
def describe_error(exc, cfg=None):
    """Traduce las excepciones de smtplib/socket a algo accionable."""
    where = f'{cfg.host}:{cfg.port}' if cfg is not None and cfg.host else 'el servidor de correo'
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return f'El servidor rechazó el usuario o la contraseña (código {exc.smtp_code}).'
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (f'El servidor rechazó el remitente «{exc.sender}»: debe ser una dirección '
                f'que la cuenta tenga permitido usar.')
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f'El servidor rechazó los destinatarios: {", ".join(exc.recipients)}.'
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return ('El servidor no admite STARTTLS o la autenticación en ese puerto: revisa el '
                'tipo de cifrado.')
    if isinstance(exc, ssl.SSLError):
        return (f'Falló la conexión cifrada con {where} ({exc.reason or exc}). Revisa el tipo '
                f'de cifrado: SSL/TLS suele ir en el puerto 465 y STARTTLS en el 587.')
    if isinstance(exc, socket.gaierror):
        return f'No se encontró el servidor «{cfg.host if cfg else ""}»: revisa el nombre.'
    if isinstance(exc, ConnectionRefusedError):
        return f'{where} rechazó la conexión: revisa el puerto.'
    if isinstance(exc, TimeoutError):
        return f'Se agotó el tiempo de espera con {where}: revisa el servidor, el puerto y el cifrado.'
    if isinstance(exc, smtplib.SMTPServerDisconnected):
        return (f'{where} cortó la conexión. Suele ser un tipo de cifrado que no corresponde '
                f'al puerto.')
    if isinstance(exc, smtplib.SMTPResponseException):
        detail = exc.smtp_error.decode(errors='replace') if isinstance(exc.smtp_error, bytes) \
            else str(exc.smtp_error)
        return f'El servidor respondió con error {exc.smtp_code}: {detail}'
    if isinstance(exc, OSError):
        return f'No se pudo conectar con {where}: {exc}'
    return f'{type(exc).__name__}: {exc}'


def send_email(cfg, subject, body, to, attachments=()):
    """Envía un correo por el servidor configurado. Lanza MailError si no sale."""
    if not cfg.is_configured:
        raise MailError('Falta configurar el servidor de correo (Informes → Servidor de correo).')
    if cfg.password_unreadable:
        raise MailError('La contraseña SMTP guardada ya no se puede leer (cambió la clave secreta '
                        'del servidor): vuelve a ingresarla en Servidor de correo.')
    if not to:
        raise MailError('No hay destinatarios.')

    connection = get_connection(
        host=cfg.host, port=cfg.port,
        username=cfg.username, password=cfg.password if cfg.username else '',
        use_tls=cfg.security == MailSettings.Security.STARTTLS,
        use_ssl=cfg.security == MailSettings.Security.SSL,
        timeout=SMTP_TIMEOUT, fail_silently=False,
    )
    msg = EmailMessage(subject=subject, body=body, from_email=cfg.from_header, to=list(to),
                       connection=connection)
    for name, content, mimetype in attachments:
        msg.attach(name, content, mimetype)
    try:
        msg.send()
    except Exception as exc:
        raise MailError(describe_error(exc, cfg)) from exc


def send_test_email(cfg, to):
    body = (
        'Este es un correo de prueba del Casino Backoffice.\n\n'
        f'Si lo recibes, el servidor de correo quedó bien configurado ({cfg.host}:{cfg.port}) '
        'y los informes programados podrán enviarse.\n'
    )
    send_email(cfg, 'Correo de prueba — Casino Backoffice', body, [to])


# =====================================================================
#  El informe como correo
# =====================================================================
def _n(value):
    return f'{value:,}'.replace(',', '.')


def build_report_email(report, d_from, d_to):
    """(asunto, cuerpo, adjuntos) del informe del período [d_from, d_to] con los filtros del envío."""
    from backend.apps.reports.excel import build_excel
    from backend.apps.reports.pdf import build_pdf
    from backend.apps.reports.service import build_report, local_range, report_title

    dt_from, dt_to = local_range(d_from, d_to)
    data = build_report(dt_from, dt_to, station=report.station, shift_name=report.shift_name)
    titulo = report_title(d_from, d_to, report.shift_name)

    stem = f'Informe_{d_from:%Y%m%d}_{d_to:%Y%m%d}'
    attachments = [(f'{stem}.pdf', build_pdf(data, titulo), 'application/pdf')]
    if report.attach_excel:
        attachments.append((f'{stem}.xlsx', build_excel(data, titulo), XLSX_MIME))

    subject = f'{(report.subject or report.name).strip()} — {titulo}'

    lines = []
    if report.message.strip():
        lines += [report.message.strip(), '']
    lines += [
        titulo,
        f'{report.filters_text} · {period_text(d_from, d_to)}',
        '',
        f'Colaciones servidas: {_n(data.servidas)}',
    ]
    if data.manuales:
        lines.append(f'  de ellas, ingreso manual: {_n(data.manuales)}')
    lines += [
        f'Personas distintas: {_n(data.personas_unicas)}',
        f'Visitas (tarjeta): {_n(data.visitas)}',
        f'Intentos duplicados: {_n(data.duplicados)}',
        f'No autorizados: {_n(data.no_autorizados)}',
        '',
        'Se adjunta el informe completo en PDF' + (' y en Excel.' if report.attach_excel else '.'),
        '',
        '--',
        f'Correo automático del Casino Backoffice: envío «{report.name}» '
        f'({report.schedule_text.lower()}).',
    ]
    return subject, '\n'.join(lines) + '\n', attachments


# =====================================================================
#  Envíos
# =====================================================================
def deliver(delivery, now=None, retry=True):
    """
    Intenta un envío y guarda el resultado. True si salió.

    Con `retry`, un fallo deja el envío en cola para reintentarlo más tarde hasta
    MAX_ATTEMPTS; sin él (envíos manuales), queda «Falló» de inmediato.
    """
    now = now or timezone.now()
    delivery.attempts += 1
    try:
        report = delivery.report
        if report is None:
            raise MailError('El envío programado se eliminó.')
        # punto de guardado: si armar el informe rompe la transacción, el resultado del
        # intento se puede guardar igual
        with transaction.atomic():
            subject, body, attachments = build_report_email(
                report, delivery.date_from, delivery.date_to)
            cfg = MailSettings.load()
        send_email(cfg, subject, body, delivery.recipient_list, attachments)
    except Exception as exc:
        if isinstance(exc, MailError):
            delivery.error = str(exc)
        else:
            # un error al armar el informe es un bug: que quede la traza en el log
            logger.exception('Error inesperado en el envío %s', delivery.pk)
            delivery.error = describe_error(exc)
        if retry and delivery.attempts < MAX_ATTEMPTS:
            delay = RETRY_DELAYS[min(delivery.attempts, len(RETRY_DELAYS)) - 1]
            delivery.status = ReportDelivery.Status.PENDING
            delivery.next_attempt_at = now + delay
        else:
            delivery.status = ReportDelivery.Status.FAILED
            delivery.next_attempt_at = None
        delivery.save()
        logger.warning('Envío %s «%s» falló (intento %s): %s', delivery.pk, delivery.report_name,
                       delivery.attempts, delivery.error)
        return False

    delivery.status = ReportDelivery.Status.SENT
    delivery.sent_at = now
    delivery.next_attempt_at = None
    delivery.error = ''
    delivery.save()
    logger.info('Envío %s «%s» enviado a %s', delivery.pk, delivery.report_name,
                ', '.join(delivery.recipient_list))
    return True


def send_now(report, user=None, now=None):
    """«Enviar ahora»: el informe del período que cubriría un envío hecho hoy, al momento."""
    now = now or timezone.now()
    d_from, d_to = report.period_for(timezone.localtime(now).date())
    delivery = ReportDelivery.objects.create(
        report=report, report_name=report.name, trigger=ReportDelivery.Trigger.MANUAL,
        scheduled_for=now, date_from=d_from, date_to=d_to, recipients=report.recipients,
        requested_by=user,
    )
    deliver(delivery, now, retry=False)
    return delivery


def retry_delivery(delivery, now=None):
    """
    Reintento a mano de un envío fallido: mismo período, con los destinatarios actuales
    del envío programado (por si el error era una dirección mal escrita y ya se corrigió).
    """
    if delivery.report is not None:
        delivery.recipients = delivery.report.recipients
    delivery.status = ReportDelivery.Status.PENDING
    return deliver(delivery, now, retry=False)


# =====================================================================
#  Programador
# =====================================================================
def enqueue_due(now):
    """Encola los envíos programados a los que les llegó la hora. Devuelve los encolados."""
    queued = []
    due = (ScheduledReport.objects.filter(is_active=True, next_run_at__lte=now)
           .values_list('pk', flat=True))
    for pk in list(due):
        with transaction.atomic():
            # skip_locked: si hubiera dos programadores, cada envío lo toma uno solo
            report = (ScheduledReport.objects.select_for_update(skip_locked=True)
                      .filter(pk=pk, is_active=True, next_run_at__lte=now).first())
            if report is None:
                continue
            occurrence = report.next_run_at
            following = report.next_run_after(occurrence)
            skipped = 0
            while following is not None and following <= now:
                occurrence, following = following, report.next_run_after(following)
                skipped += 1
            if skipped:
                logger.warning('Envío «%s»: se omitieron %s fechas atrasadas; se envía la del %s',
                               report.name, skipped, timezone.localtime(occurrence))

            d_from, d_to = report.period_for(timezone.localtime(occurrence).date())
            delivery, created = ReportDelivery.objects.get_or_create(
                report=report, scheduled_for=occurrence, trigger=ReportDelivery.Trigger.SCHEDULE,
                defaults={'report_name': report.name, 'date_from': d_from, 'date_to': d_to,
                          'recipients': report.recipients, 'next_attempt_at': now},
            )
            report.next_run_at = following
            report.save(update_fields=['next_run_at'])
            if created:
                queued.append(delivery)
    return queued


def process_pending(now):
    """Intenta los envíos en cola cuyo turno llegó. Devuelve cuántos salieron."""
    sent = 0
    pending = (ReportDelivery.objects
               .filter(status=ReportDelivery.Status.PENDING, next_attempt_at__lte=now)
               .order_by('next_attempt_at', 'pk').values_list('pk', flat=True))
    for pk in list(pending):
        with transaction.atomic():
            delivery = (ReportDelivery.objects.select_for_update(skip_locked=True)
                        .filter(pk=pk, status=ReportDelivery.Status.PENDING).first())
            if delivery is None:
                continue
            if deliver(delivery, now):
                sent += 1
    return sent


def run_scheduler_tick(now=None):
    """Un ciclo del programador: señal de vida, encolar lo que toca y despachar la cola."""
    now = now or timezone.now()
    MailSettings.touch_scheduler(now)
    enqueue_due(now)
    return process_pending(now)
