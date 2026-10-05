"""
Envío programado del informe por correo: calendario y período de cada envío, el
programador (cola, reintentos, fechas atrasadas), el servidor SMTP y las pantallas.

El correo no sale a ninguna parte: en las pruebas Django usa el backend en memoria
(`mail.outbox`), salvo donde se prueba el backend SMTP real con smtplib simulado.
"""
import smtplib
from datetime import date, datetime, time, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Role, Shift, Station
from backend.apps.reports.mailing import (
    MAX_ATTEMPTS, retry_delivery, run_scheduler_tick, send_now,
)
from backend.apps.reports.models import MailSettings, ReportDelivery, ScheduledReport

User = get_user_model()
F = ScheduledReport.Frequency
P = ScheduledReport.Period

MON, TUE, WED, THU = 0, 1, 2, 3


def _at(d, hh=0, mm=0, ss=0):
    return timezone.make_aware(datetime.combine(d, time(hh, mm, ss)), timezone.get_current_timezone())


def _report(**kw):
    defaults = {'name': 'Semanal', 'frequency': F.WEEKLY, 'weekdays_mask': 1 << MON,
                'send_time': time(8, 0), 'period': P.PREVIOUS_WEEK,
                'recipients': 'gerencia@test.cl, contabilidad@test.cl'}
    defaults.update(kw)
    return ScheduledReport(**defaults)


def _configure_smtp(**kw):
    cfg = MailSettings.load()
    cfg.host = kw.get('host', 'smtp.test.cl')
    cfg.port = kw.get('port', 587)
    cfg.security = kw.get('security', MailSettings.Security.STARTTLS)
    cfg.username = kw.get('username', 'informes@test.cl')
    cfg.set_password(kw.get('password', 'secreta'))
    cfg.from_email = 'informes@test.cl'
    cfg.from_name = 'Casino Backoffice'
    cfg.save()
    return cfg


# =====================================================================
#  Calendario: cuándo toca y qué período cubre
# =====================================================================
class ScheduleCalendarTests(TestCase):
    # lunes 5 de octubre de 2026
    MONDAY = date(2026, 10, 5)

    def test_semanal_los_lunes(self):
        r = _report()
        domingo = self.MONDAY - timedelta(days=1)
        self.assertEqual(r.next_run_after(_at(domingo, 10)), _at(self.MONDAY, 8))
        # justo a la hora ya no cuenta: el siguiente es el lunes que viene
        self.assertEqual(r.next_run_after(_at(self.MONDAY, 8)), _at(self.MONDAY + timedelta(days=7), 8))
        self.assertEqual(r.next_run_after(_at(self.MONDAY, 7, 59)), _at(self.MONDAY, 8))

    def test_varios_dias_de_la_semana(self):
        r = _report(weekdays_mask=(1 << MON) | (1 << THU))
        self.assertEqual(r.next_run_after(_at(self.MONDAY, 9)), _at(self.MONDAY + timedelta(days=3), 8))
        self.assertEqual(r.schedule_text, 'Cada lunes y jueves a las 08:00')

    def test_diario(self):
        r = _report(frequency=F.DAILY, send_time=time(23, 30))
        self.assertEqual(r.next_run_after(_at(self.MONDAY, 23, 31)), _at(self.MONDAY + timedelta(days=1), 23, 30))
        self.assertEqual(r.schedule_text, 'Todos los días a las 23:30')

    def test_mensual_el_24(self):
        r = _report(frequency=F.MONTHLY, month_day=24)
        self.assertEqual(r.next_run_after(_at(self.MONDAY, 8)), _at(date(2026, 10, 24), 8))
        self.assertEqual(r.next_run_after(_at(date(2026, 10, 24), 8)), _at(date(2026, 11, 24), 8))
        self.assertEqual(r.schedule_text, 'El día 24 de cada mes a las 08:00')

    def test_mensual_dia_31_en_meses_cortos_cae_el_ultimo_dia(self):
        r = _report(frequency=F.MONTHLY, month_day=31)
        self.assertEqual(r.next_run_after(_at(date(2027, 2, 1))), _at(date(2027, 2, 28), 8))
        self.assertEqual(r.next_run_after(_at(date(2026, 11, 1))), _at(date(2026, 11, 30), 8))
        self.assertEqual(r.schedule_text, 'El último día de cada mes a las 08:00')

    def test_semanal_sin_dias_no_programa_nada(self):
        self.assertIsNone(_report(weekdays_mask=0).next_run_after(_at(self.MONDAY)))

    def test_periodos(self):
        lunes = self.MONDAY
        self.assertEqual(_report(period=P.PREVIOUS_DAY).period_for(lunes),
                         (date(2026, 10, 4), date(2026, 10, 4)))
        self.assertEqual(_report(period=P.SAME_DAY).period_for(lunes), (lunes, lunes))
        # semana anterior de lunes a domingo, aunque el envío sea a mitad de semana
        for d in (lunes, lunes + timedelta(days=3)):
            self.assertEqual(_report(period=P.PREVIOUS_WEEK).period_for(d),
                             (date(2026, 9, 28), date(2026, 10, 4)))
        self.assertEqual(_report(period=P.PREVIOUS_MONTH).period_for(date(2026, 10, 24)),
                         (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(_report(period=P.PREVIOUS_MONTH).period_for(date(2026, 3, 1)),
                         (date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(_report(period=P.LAST_DAYS, period_days=7).period_for(lunes),
                         (date(2026, 9, 28), date(2026, 10, 4)))

    def test_desde_el_envio_anterior(self):
        # mensual el 24: del 24 del mes anterior al 23
        r = _report(frequency=F.MONTHLY, month_day=24, period=P.SINCE_LAST)
        self.assertEqual(r.period_for(date(2026, 10, 24)), (date(2026, 9, 24), date(2026, 10, 23)))
        # lunes y jueves: el del jueves cubre lunes a miércoles; el del lunes, jueves a domingo
        r = _report(weekdays_mask=(1 << MON) | (1 << THU), period=P.SINCE_LAST)
        self.assertEqual(r.period_for(date(2026, 10, 8)), (date(2026, 10, 5), date(2026, 10, 7)))
        self.assertEqual(r.period_for(date(2026, 10, 12)), (date(2026, 10, 8), date(2026, 10, 11)))
        # diario: el día anterior
        self.assertEqual(_report(frequency=F.DAILY, period=P.SINCE_LAST).period_for(date(2026, 10, 5)),
                         (date(2026, 10, 4), date(2026, 10, 4)))

    def test_en_pausa_no_tiene_proximo_envio(self):
        r = _report(is_active=False)
        r.schedule_next_run(_at(self.MONDAY))
        self.assertIsNone(r.next_run_at)


# =====================================================================
#  Programador: encolar, enviar, reintentar
# =====================================================================
class SchedulerTests(TestCase):
    MONDAY = date(2026, 10, 5)

    def setUp(self):
        _configure_smtp()
        self.station = Station.objects.create(name='Casino', device_id=1000)
        # semana anterior al lunes 5: tres colaciones el miércoles 30-09
        wed = date(2026, 9, 30)
        shift = Shift.objects.create(station=self.station, uid='alm', remote_id=1, name='Almuerzo',
                                     started_at=_at(wed, 12), ended_at=_at(wed, 14), service_date=wed)
        for i in range(3):
            AccessEvent.objects.create(
                station=self.station, uid=f'e{i}', remote_id=i, shift=shift, employee_no=str(i),
                person_name=f'P{i}', company='ACME', event_time=_at(wed, 12, 10 + i),
                status=AccessEvent.Status.OK)
        self.report = _report(next_run_at=_at(self.MONDAY, 8))
        self.report.save()

    def test_envia_el_informe_de_la_semana_anterior_con_el_pdf(self):
        run_scheduler_tick(_at(self.MONDAY, 8, 0, 20))

        d = ReportDelivery.objects.get()
        self.assertEqual(d.status, ReportDelivery.Status.SENT)
        self.assertEqual((d.date_from, d.date_to), (date(2026, 9, 28), date(2026, 10, 4)))
        self.assertEqual(d.scheduled_for, _at(self.MONDAY, 8))

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ['gerencia@test.cl', 'contabilidad@test.cl'])
        self.assertEqual(msg.subject, 'Semanal — Informe 28-09-2026 al 04-10-2026')
        self.assertIn('Colaciones servidas: 3', msg.body)
        self.assertIn('informes@test.cl', msg.from_email)
        [(name, content, mimetype)] = msg.attachments
        self.assertEqual((name, mimetype), ('Informe_20260928_20261004.pdf', 'application/pdf'))
        self.assertTrue(content.startswith(b'%PDF'))

        self.report.refresh_from_db()
        self.assertEqual(self.report.next_run_at, _at(self.MONDAY + timedelta(days=7), 8))

    def test_no_envia_antes_de_la_hora_ni_dos_veces(self):
        run_scheduler_tick(_at(self.MONDAY, 7, 59))
        self.assertEqual(ReportDelivery.objects.count(), 0)
        run_scheduler_tick(_at(self.MONDAY, 8, 0, 10))
        run_scheduler_tick(_at(self.MONDAY, 8, 0, 40))
        self.assertEqual(ReportDelivery.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_filtros_y_excel(self):
        otra = Station.objects.create(name='Otra', device_id=1001)
        ScheduledReport.objects.filter(pk=self.report.pk).update(station=otra, attach_excel=True)
        run_scheduler_tick(_at(self.MONDAY, 8, 1))
        msg = mail.outbox[0]
        self.assertIn('Colaciones servidas: 0', msg.body)   # la otra estación no tuvo colaciones
        self.assertIn('Otra · todos los turnos', msg.body)
        self.assertEqual([a[0] for a in msg.attachments],
                         ['Informe_20260928_20261004.pdf', 'Informe_20260928_20261004.xlsx'])

    def test_en_pausa_no_se_envia(self):
        ScheduledReport.objects.filter(pk=self.report.pk).update(is_active=False)
        run_scheduler_tick(_at(self.MONDAY, 9))
        self.assertEqual(ReportDelivery.objects.count(), 0)

    def test_fechas_atrasadas_envia_solo_la_mas_reciente(self):
        """Programador detenido tres semanas: un solo envío, el de la fecha más reciente."""
        ScheduledReport.objects.filter(pk=self.report.pk).update(
            next_run_at=_at(self.MONDAY - timedelta(days=21), 8))
        run_scheduler_tick(_at(self.MONDAY, 10))
        d = ReportDelivery.objects.get()
        self.assertEqual(d.scheduled_for, _at(self.MONDAY, 8))
        self.assertEqual((d.date_from, d.date_to), (date(2026, 9, 28), date(2026, 10, 4)))
        self.report.refresh_from_db()
        self.assertEqual(self.report.next_run_at, _at(self.MONDAY + timedelta(days=7), 8))

    def test_reintenta_si_falla_el_servidor_y_luego_da_por_fallido(self):
        error = smtplib.SMTPAuthenticationError(535, b'5.7.3 Authentication unsuccessful')
        with mock.patch('django.core.mail.EmailMessage.send', side_effect=error):
            run_scheduler_tick(_at(self.MONDAY, 8, 0, 30))
            d = ReportDelivery.objects.get()
            self.assertEqual((d.status, d.attempts), (ReportDelivery.Status.PENDING, 1))
            self.assertIn('rechazó el usuario o la contraseña', d.error)
            self.assertEqual(d.next_attempt_at, _at(self.MONDAY, 8, 10, 30))

            run_scheduler_tick(_at(self.MONDAY, 8, 5))       # aún no le toca
            d.refresh_from_db()
            self.assertEqual(d.attempts, 1)

            run_scheduler_tick(_at(self.MONDAY, 8, 11))
            d.refresh_from_db()
            self.assertEqual((d.status, d.attempts), (ReportDelivery.Status.PENDING, 2))
            self.assertEqual(d.next_attempt_at, _at(self.MONDAY, 8, 41))

            run_scheduler_tick(_at(self.MONDAY, 8, 42))
            d.refresh_from_db()
            self.assertEqual((d.status, d.attempts), (ReportDelivery.Status.FAILED, MAX_ATTEMPTS))
            self.assertIsNone(d.next_attempt_at)

        # reintento a mano, ya con el servidor bien: mismo período, sale
        self.assertTrue(retry_delivery(d))
        d.refresh_from_db()
        self.assertEqual(d.status, ReportDelivery.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)

    def test_sin_servidor_configurado_falla_con_mensaje_claro(self):
        MailSettings.objects.filter(pk=1).update(host='')
        run_scheduler_tick(_at(self.MONDAY, 8, 1))
        d = ReportDelivery.objects.get()
        self.assertIn('Falta configurar el servidor de correo', d.error)
        self.assertEqual(len(mail.outbox), 0)

    def test_eliminar_el_envio_conserva_el_historial(self):
        run_scheduler_tick(_at(self.MONDAY, 8, 1))
        self.report.delete()
        d = ReportDelivery.objects.get()
        self.assertIsNone(d.report)
        self.assertEqual(d.report_name, 'Semanal')

    def test_enviar_ahora(self):
        d = send_now(self.report, now=_at(self.MONDAY + timedelta(days=2), 15))
        self.assertEqual(d.status, ReportDelivery.Status.SENT)
        self.assertEqual(d.trigger, ReportDelivery.Trigger.MANUAL)
        self.assertEqual((d.date_from, d.date_to), (date(2026, 9, 28), date(2026, 10, 4)))
        # no toca la programación
        self.report.refresh_from_db()
        self.assertEqual(self.report.next_run_at, _at(self.MONDAY, 8))

    def test_comando_y_senal_de_vida(self):
        self.assertFalse(MailSettings.load().scheduler_running)
        call_command('send_scheduled_reports', stdout=mock.MagicMock())
        self.assertTrue(MailSettings.load().scheduler_running)


# =====================================================================
#  Servidor SMTP
# =====================================================================
class MailServerTests(TestCase):
    def test_la_contrasena_se_guarda_cifrada(self):
        cfg = _configure_smtp(password='Clave-SMTP-123')
        cfg.refresh_from_db()
        self.assertNotIn('Clave-SMTP-123', cfg.password_encrypted)
        self.assertEqual(cfg.password, 'Clave-SMTP-123')
        self.assertFalse(cfg.password_unreadable)

    def test_si_cambia_la_clave_secreta_la_contrasena_queda_ilegible(self):
        cfg = _configure_smtp()
        with override_settings(SECRET_KEY='otra-clave-secreta'):
            self.assertTrue(cfg.password_unreadable)
            self.assertEqual(cfg.password, '')

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend')
    def test_usa_el_backend_smtp_con_los_datos_configurados(self):
        from backend.apps.reports.mailing import send_test_email

        cfg = _configure_smtp(host='smtp.office365.com', port=587, password='p4ss')
        with mock.patch('smtplib.SMTP') as smtp:
            send_test_email(cfg, 'yo@test.cl')
        smtp.assert_called_once_with('smtp.office365.com', 587, local_hostname=mock.ANY, timeout=30)
        conn = smtp.return_value
        conn.starttls.assert_called_once()
        conn.login.assert_called_once_with('informes@test.cl', 'p4ss')
        conn.sendmail.assert_called_once()
        self.assertEqual(conn.sendmail.call_args.args[1], ['yo@test.cl'])

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend')
    def test_ssl_sin_autenticacion(self):
        from backend.apps.reports.mailing import send_test_email

        cfg = _configure_smtp(host='mail.test.cl', port=465, security=MailSettings.Security.SSL,
                              username='')
        cfg.set_password('')
        cfg.save()
        with mock.patch('smtplib.SMTP_SSL') as smtp_ssl:
            send_test_email(cfg, 'yo@test.cl')
        smtp_ssl.assert_called_once()
        smtp_ssl.return_value.login.assert_not_called()

    @override_settings(EMAIL_BACKEND='django.core.mail.backends.smtp.EmailBackend')
    def test_error_de_conexion_se_explica(self):
        from backend.apps.reports.mailing import MailError, send_test_email

        cfg = _configure_smtp(host='noexiste.test.cl')
        with mock.patch('smtplib.SMTP', side_effect=ConnectionRefusedError(111, 'refused')):
            with self.assertRaisesMessage(MailError, 'noexiste.test.cl:587 rechazó la conexión'):
                send_test_email(cfg, 'yo@test.cl')


# =====================================================================
#  Pantallas
# =====================================================================
class MailViewsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email='admin@test.cl', password='x', first_name='A', last_name='A',
            role=Role.objects.get(code='admin'))
        self.client.force_login(self.admin)

    def _post_report(self, **kw):
        data = {'name': 'Cierre mensual', 'is_active': 'on', 'frequency': 'monthly',
                'month_day': '24', 'send_time': '08:00', 'period': 'since_last', 'period_days': '7',
                'station': '', 'shift_name': '',
                'recipients': 'gerencia@test.cl; contabilidad@test.cl\nGERENCIA@test.cl',
                'subject': '', 'message': ''}
        data.update(kw)
        return self.client.post('/reportes/envios/nuevo/', data)

    def test_crear_envio_mensual(self):
        resp = self._post_report()
        self.assertRedirects(resp, '/reportes/envios/')
        r = ScheduledReport.objects.get()
        self.assertEqual(r.recipients, 'gerencia@test.cl, contabilidad@test.cl')   # sin repetir
        self.assertEqual(r.created_by, self.admin)
        self.assertEqual(timezone.localtime(r.next_run_at).day, 24)
        self.assertGreater(r.next_run_at, timezone.now())

        resp = self.client.get('/reportes/envios/')
        self.assertContains(resp, 'Cierre mensual')
        self.assertContains(resp, 'El día 24 de cada mes a las 08:00')

    def test_valida_destinatarios_y_dias(self):
        resp = self._post_report(recipients='bien@test.cl, mal@')
        self.assertContains(resp, 'Direcciones no válidas: mal@')
        resp = self._post_report(frequency='weekly')
        self.assertContains(resp, 'Marca al menos un día de la semana.')
        resp = self._post_report(frequency='weekly', weekdays=['0', '3'])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(ScheduledReport.objects.get().weekdays, [0, 3])

    def test_pausar_quita_el_proximo_envio(self):
        self._post_report()
        r = ScheduledReport.objects.get()
        data = {'name': r.name, 'frequency': 'monthly', 'month_day': '24', 'send_time': '08:00',
                'period': 'since_last', 'recipients': r.recipients}   # sin is_active = en pausa
        self.client.post(f'/reportes/envios/{r.pk}/editar/', data)
        r.refresh_from_db()
        self.assertFalse(r.is_active)
        self.assertIsNone(r.next_run_at)

    def test_enviar_ahora_desde_la_pantalla(self):
        _configure_smtp()
        self._post_report()
        r = ScheduledReport.objects.get()
        self.assertEqual(self.client.get(f'/reportes/envios/{r.pk}/enviar/').status_code, 405)
        resp = self.client.post(f'/reportes/envios/{r.pk}/enviar/', follow=True)
        self.assertContains(resp, 'enviado a 2 destinatario(s)')
        self.assertEqual(len(mail.outbox), 1)
        self.assertContains(self.client.get('/reportes/envios/historial/'), 'Cierre mensual')

    def test_reintentar_un_envio_fallido(self):
        self._post_report()
        r = ScheduledReport.objects.get()
        resp = self.client.post(f'/reportes/envios/{r.pk}/enviar/', follow=True)   # sin SMTP
        self.assertContains(resp, 'Falta configurar el servidor de correo')
        d = ReportDelivery.objects.get()
        self.assertEqual(d.status, ReportDelivery.Status.FAILED)

        _configure_smtp()
        resp = self.client.post(f'/reportes/envios/historial/{d.pk}/reintentar/',
                                {'next': 'https://malicioso.example/'})
        self.assertRedirects(resp, '/reportes/envios/historial/')   # el next externo se ignora
        d.refresh_from_db()
        self.assertEqual(d.status, ReportDelivery.Status.SENT)

    def test_configurar_smtp_y_enviar_prueba(self):
        resp = self.client.post('/reportes/correo/', {
            'host': 'smtp.test.cl', 'port': '587', 'security': 'starttls',
            'username': 'informes@test.cl', 'password': 'secreta',
            'from_email': 'informes@test.cl', 'from_name': 'Casino'})
        self.assertRedirects(resp, '/reportes/correo/')
        cfg = MailSettings.load()
        self.assertEqual((cfg.host, cfg.password, cfg.updated_by), ('smtp.test.cl', 'secreta', self.admin))

        # la contraseña nunca vuelve a la página, y en blanco se conserva
        resp = self.client.get('/reportes/correo/')
        self.assertNotContains(resp, 'secreta')
        self.client.post('/reportes/correo/', {
            'host': 'smtp.test.cl', 'port': '2525', 'security': 'starttls',
            'username': 'informes@test.cl', 'password': '',
            'from_email': 'informes@test.cl', 'from_name': 'Casino'})
        cfg.refresh_from_db()
        self.assertEqual((cfg.port, cfg.password), (2525, 'secreta'))

        resp = self.client.post('/reportes/correo/prueba/', {'to': 'yo@test.cl'}, follow=True)
        self.assertContains(resp, 'Correo de prueba enviado a yo@test.cl')
        self.assertEqual(mail.outbox[0].to, ['yo@test.cl'])

    def test_smtp_con_usuario_exige_contrasena(self):
        resp = self.client.post('/reportes/correo/', {
            'host': 'smtp.test.cl', 'port': '587', 'security': 'starttls',
            'username': 'informes@test.cl', 'password': '', 'from_email': 'informes@test.cl'})
        self.assertContains(resp, 'Ingresa la contraseña de la cuenta.')

    def test_rol_solo_con_envios_por_correo(self):
        """Un rol que solo programa envíos entra a ellos, pero no al informe ni al SMTP."""
        rol = Role.objects.create(name='Envíos', permissions=['report_mail'])
        user = User.objects.create_user(email='env@test.cl', password='x', first_name='E',
                                        last_name='E', role=rol)
        self.client.force_login(user)
        resp = self.client.get('/', follow=True)
        self.assertEqual(resp.request['PATH_INFO'], '/reportes/envios/')
        self.assertContains(resp, 'Envíos por correo')
        self.assertNotContains(resp, 'Servidor de correo</a>')
        self.assertEqual(self.client.get('/reportes/correo/').status_code, 302)
        self.assertEqual(self.client.get('/reportes/').status_code, 302)
