"""
Ingreso manual de colaciones desde el backoffice (el kiosco no pudo marcarlas) y su
rastro: marcada como manual en el detalle y en el informe, anulable y en la bitácora.
"""
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.utils import timezone

from backend.apps.core.models import (
    AccessEvent, AuditLog, Person, Role, Shift, ShiftSchedule, ShiftScheduleCompany, Station,
)
from backend.apps.reports import manual
from backend.apps.reports.excel import build_excel, build_shift_excel
from backend.apps.reports.pdf import build_pdf
from backend.apps.reports.service import build_report
from backend.apps.webapp.permissions import DENIED_MESSAGE


def _at(day, hh, mm=0, ss=0):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm, second=ss))


class ManualEventTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            email='admin@test.cl', password='x', first_name='Jose', last_name='Admin',
            role=Role.objects.get(code='admin'))
        self.gerente = User.objects.create_user(
            email='g@test.cl', password='x', first_name='G', last_name='G',
            role=Role.objects.get(code='gerente'))
        self.station = Station.objects.create(name='Casino', device_id=1000)
        self.day = timezone.localdate() - timedelta(days=1)
        # Desayuno 06:00–09:00, solo PPE y PTJ
        self.schedule = ShiftSchedule.objects.create(
            station=self.station, uid='sch-des', name='Desayuno', start_min=360, end_min=540,
            all_companies=False)
        for c in ('PPE', 'PTJ'):
            ShiftScheduleCompany.objects.create(schedule=self.schedule, company=c)
        # la apertura real: interrumpida por el corte, cerrada por el terminal al volver
        self.shift = Shift.objects.create(
            station=self.station, uid='des-1', remote_id=1, name='Desayuno',
            started_at=_at(self.day, 6, 1, 36), ended_at=_at(self.day, 9, 1, 54),
            service_date=self.day, schedule_uid='sch-des',
            end_reason=Shift.EndReason.INTERRUPTED)
        self.ana = Person.objects.create(station=self.station, employee_no='8458387', name='Ana Pérez',
                                         company='PPE', meal_policy=Person.MealPolicy.ALL_SHIFTS)
        self.pedro = Person.objects.create(station=self.station, employee_no='10121385', name='Pedro Muñoz',
                                           company='PTJ', meal_policy=Person.MealPolicy.ALL_SHIFTS)
        self.externo = Person.objects.create(station=self.station, employee_no='555', name='Externo Uno',
                                             company='OTRA', authorized=False)
        Person.objects.create(station=self.station, employee_no='card:001', name='Visita 01',
                              user_type='visitor')
        # Ana sí alcanzó a marcar antes del corte
        AccessEvent.objects.create(
            station=self.station, uid='ev1', remote_id=1, shift=self.shift, employee_no='8458387',
            person_name='Ana Pérez', company='PPE', verify_method='Rostro',
            event_time=_at(self.day, 6, 9, 11), status=AccessEvent.Status.OK)
        self.url = f'/reportes/turnos/{self.shift.pk}/ingreso-manual/'
        self.client.force_login(self.admin)

    def _post(self, person, hora='08:30:00', reason='Corte de energía, kiosco apagado; anotado en hoja',
              override=False, confirm=True):
        data = {'person': person.pk, 'event_time': hora, 'reason': reason}
        if override:
            data['override'] = 'on'
        if confirm:
            data['confirm'] = 'on'
        return self.client.post(self.url, data)

    # ---- acceso ----
    def test_solo_quien_tiene_el_permiso_entra(self):
        self.client.force_login(self.gerente)
        resp = self.client.get(self.url, follow=True)
        self.assertIn(DENIED_MESSAGE, [str(m) for m in get_messages(resp.wsgi_request)])
        resp = self._post(self.pedro)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(AccessEvent.objects.filter(employee_no='10121385').exists())

        # el detalle del turno no ofrece el botón a quien no puede
        resp = self.client.get(f'/reportes/turnos/{self.shift.pk}/')
        self.assertNotContains(resp, 'Ingresar colación')
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(f'/reportes/turnos/{self.shift.pk}/'), 'Ingresar colación')

    # ---- registro ----
    def test_registra_la_colacion_marcada_como_manual_y_en_la_bitacora(self):
        resp = self._post(self.pedro)
        self.assertRedirects(resp, self.url)

        ev = AccessEvent.objects.get(employee_no='10121385')
        self.assertEqual(ev.status, AccessEvent.Status.OK)
        self.assertEqual(ev.origin, AccessEvent.Origin.BACKOFFICE)
        self.assertTrue(ev.is_manual)
        self.assertEqual(ev.shift, self.shift)
        self.assertEqual((ev.person_name, ev.company), ('Pedro Muñoz', 'PTJ'))
        self.assertEqual(timezone.localtime(ev.event_time).strftime('%H:%M:%S'), '08:30:00')
        self.assertEqual(ev.entered_by, self.admin)
        self.assertEqual(ev.entered_by_name, 'Jose Admin')
        self.assertIn('Corte de energía', ev.entry_reason)
        self.assertTrue(ev.uid)          # identidad propia: el terminal jamás la pisará
        self.assertIn('Manual', ev.verify_method)

        entry = AuditLog.objects.get(action='colacion.ingreso_manual')
        self.assertEqual((entry.user, entry.shift, entry.event, entry.station), (self.admin, self.shift, ev, self.station))
        self.assertIn('Pedro Muñoz', entry.summary)
        self.assertIn('Corte de energía', entry.summary)
        self.assertEqual(entry.category, 'colacion')

        # el formulario vuelve listo para la siguiente persona, con la registrada a la vista
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Pedro Muñoz')
        self.assertContains(resp, 'Jose Admin')

    def test_no_duplica_a_quien_ya_tiene_colacion(self):
        resp = self._post(self.ana)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ya tiene una colación válida')
        self.assertEqual(AccessEvent.objects.filter(employee_no='8458387').count(), 1)
        self.assertFalse(AuditLog.objects.filter(action='colacion.ingreso_manual').exists())

        # tampoco si la anterior fue manual
        self._post(self.pedro)
        resp = self._post(self.pedro, hora='08:40:00')
        self.assertContains(resp, 'ingreso manual')
        self.assertEqual(AccessEvent.objects.filter(employee_no='10121385').count(), 1)

    def test_una_colacion_por_servicio_cuenta_las_reaperturas(self):
        reopen = Shift.objects.create(
            station=self.station, uid='des-2', remote_id=2, name='Desayuno',
            started_at=_at(self.day, 9, 5), ended_at=_at(self.day, 9, 15), service_date=self.day,
            schedule_uid='sch-des', reopened_from_uid='des-1')
        AccessEvent.objects.create(
            station=self.station, uid='ev2', remote_id=2, shift=reopen, employee_no='10121385',
            person_name='Pedro Muñoz', company='PTJ', event_time=_at(self.day, 9, 6),
            status=AccessEvent.Status.OK)
        resp = self._post(self.pedro)
        self.assertContains(resp, 'una apertura del mismo servicio')

    def test_la_hora_debe_caer_dentro_del_turno(self):
        resp = self._post(self.pedro, hora='05:30:00')
        self.assertContains(resp, 'La hora debe estar dentro del turno')
        resp = self._post(self.pedro, hora='10:00:00')
        self.assertContains(resp, 'La hora debe estar dentro del turno')
        self.assertFalse(AccessEvent.objects.filter(employee_no='10121385').exists())

    def test_ventana_llega_hasta_el_termino_programado(self):
        """Un turno cerrado antes de su horario acepta colaciones hasta la hora programada."""
        Shift.objects.filter(pk=self.shift.pk).update(ended_at=_at(self.day, 7, 0),
                                                      end_reason=Shift.EndReason.MANUAL)
        self.shift.refresh_from_db()
        start, end = manual.shift_window(self.shift)
        self.assertEqual(timezone.localtime(end).strftime('%H:%M'), '09:00')
        self.assertRedirects(self._post(self.pedro, hora='08:30:00'), self.url)

    def test_motivo_y_confirmacion_obligatorios(self):
        resp = self._post(self.pedro, reason='corto')
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(AccessEvent.objects.filter(employee_no='10121385').exists())
        resp = self._post(self.pedro, confirm=False)
        self.assertContains(resp, 'Debes confirmar')
        self.assertFalse(AccessEvent.objects.filter(employee_no='10121385').exists())

    def test_los_avisos_exigen_confirmacion_expresa(self):
        """Lo que el terminal habría rechazado se puede registrar, pero diciéndolo."""
        resp = self._post(self.externo)
        self.assertContains(resp, 'Registrar de todos modos')
        self.assertFalse(AccessEvent.objects.filter(employee_no='555').exists())

        resp = self._post(self.externo, override=True)
        self.assertRedirects(resp, self.url)
        self.assertTrue(AccessEvent.objects.filter(employee_no='555', status='Ok').exists())
        entry = AuditLog.objects.get(action='colacion.ingreso_manual')
        self.assertEqual(entry.level, AuditLog.Level.WARNING)
        self.assertIn('pese a los avisos', entry.summary)
        self.assertIn('NO autorizada', entry.summary)
        self.assertIn('OTRA no está autorizada', entry.summary)

    def test_turno_abierto_o_de_ingreso_manual_no_admite(self):
        Shift.objects.filter(pk=self.shift.pk).update(ended_at=None, end_reason='')
        resp = self.client.get(self.url)
        self.assertContains(resp, 'sigue en curso')
        self._post(self.pedro)
        self.assertFalse(AccessEvent.objects.filter(employee_no='10121385').exists())

        once = Shift.objects.create(
            station=self.station, uid='once', remote_id=9, name='Once',
            started_at=_at(self.day, 16), ended_at=_at(self.day, 16), service_date=self.day,
            end_reason=Shift.EndReason.MANUAL_ENTRY, manual_count=10)
        resp = self.client.get(f'/reportes/turnos/{once.pk}/ingreso-manual/')
        self.assertContains(resp, 'registro de ingreso manual de la cocinera')
        self.assertNotContains(self.client.get(f'/reportes/turnos/{once.pk}/'), 'Ingresar colación')

    def test_una_visita_no_se_ingresa_a_mano(self):
        visita = Person.objects.get(employee_no='card:001')
        resp = self._post(visita)
        self.assertContains(resp, 'tarjeta de visita')
        self.assertFalse(AccessEvent.objects.filter(employee_no='card:001').exists())

    # ---- búsqueda de personas ----
    def test_busqueda_por_nombre_sin_tildes_y_por_rut(self):
        url = f'{self.url}personas/'
        rows = self.client.get(url, {'q': 'munoz'}).json()['results']
        self.assertEqual([r['name'] for r in rows], ['Pedro Muñoz'])
        self.assertEqual(rows[0]['warnings'], [])
        self.assertEqual(rows[0]['blocked'], [])

        # RUT con puntos, guion y dígito verificador: el nº de HikCentral no lleva el DV
        rows = self.client.get(url, {'q': '10.121.385-3'}).json()['results']
        self.assertEqual([r['employee_no'] for r in rows], ['10121385'])
        rows = self.client.get(url, {'q': '8458387'}).json()['results']
        self.assertEqual(rows[0]['name'], 'Ana Pérez')
        self.assertTrue(rows[0]['blocked'])     # ya tiene colación

        rows = self.client.get(url, {'q': 'externo'}).json()['results']
        self.assertEqual(len(rows[0]['warnings']), 2)   # no autorizada + empresa fuera del turno
        self.assertEqual(self.client.get(url, {'q': 'visita'}).json()['results'], [])
        self.assertEqual(self.client.get(url, {'q': 'x'}).json()['results'], [])

    # ---- rastro en detalle e informe ----
    def test_detalle_e_informe_la_marcan_como_manual(self):
        self._post(self.pedro)
        resp = self.client.get(f'/reportes/turnos/{self.shift.pk}/')
        self.assertContains(resp, 'Manual (backoffice)')
        self.assertContains(resp, 'Registrada en el backoffice por Jose Admin')
        self.assertEqual((resp.context['n_ok'], resp.context['n_manual']), (2, 1))

        resp = self.client.get(f'/reportes/turnos/?from={self.day:%Y-%m-%d}&to={self.day:%Y-%m-%d}')
        self.assertContains(resp, '1 a mano')
        self.assertEqual({s.n_ok for s in resp.context['shifts']}, {2})

        data = build_report(_at(self.day, 0), _at(self.day + timedelta(days=1), 0))
        self.assertEqual((data.servidas, data.backoffice, data.personas_unicas), (2, 1, 2))
        self.assertEqual([e.employee_no for e in data.backoffice_detalle], ['10121385'])
        row = data.por_turno[0]
        self.assertEqual((row.en_turno, row.backoffice, row.total), (2, 1, 2))
        self.assertIn(('PTJ', 1, [1]), data.por_empresa)

        resp = self.client.get(f'/reportes/?from={self.day:%Y-%m-%d}&to={self.day:%Y-%m-%d}')
        self.assertContains(resp, 'Colaciones ingresadas a mano')
        self.assertContains(resp, 'Jose Admin')
        self.assertTrue(build_pdf(data, 'Informe').startswith(b'%PDF'))
        self.assertTrue(build_excel(data, 'Informe').startswith(b'PK'))
        events = list(self.shift.events.order_by('event_time'))
        self.assertTrue(build_shift_excel(self.shift, events).startswith(b'PK'))

        resp = self.client.get('/reportes/ingresos-manuales/')
        self.assertContains(resp, 'Pedro Muñoz')
        self.assertEqual(resp.context['n_validas'], 1)

    # ---- anulación ----
    def test_anular_conserva_la_fila_y_deja_de_contar(self):
        self._post(self.pedro)
        ev = AccessEvent.objects.get(employee_no='10121385')
        resp = self.client.post(f'/reportes/ingresos-manuales/{ev.pk}/anular/',
                                {'reason': 'Se registró a la persona equivocada', 'next': self.url})
        self.assertRedirects(resp, self.url)
        ev.refresh_from_db()
        self.assertEqual(ev.status, AccessEvent.Status.ANULADO)
        self.assertTrue(ev.is_annulled)
        self.assertEqual(ev.annulled_by_name, 'Jose Admin')
        self.assertIn('equivocada', ev.annul_reason)
        self.assertIsNotNone(ev.annulled_at)

        data = build_report(_at(self.day, 0), _at(self.day + timedelta(days=1), 0))
        self.assertEqual((data.servidas, data.backoffice), (1, 0))
        resp = self.client.get(f'/reportes/turnos/{self.shift.pk}/')
        self.assertContains(resp, 'Anulada')
        self.assertEqual((resp.context['n_ok'], resp.context['n_anuladas']), (1, 1))
        entry = AuditLog.objects.get(action='colacion.anulada')
        self.assertIn('equivocada', entry.summary)

        # ya anulada: no se vuelve a anular; y la persona puede registrarse de nuevo
        self.client.post(f'/reportes/ingresos-manuales/{ev.pk}/anular/', {'reason': 'otra vez'})
        self.assertEqual(AuditLog.objects.filter(action='colacion.anulada').count(), 1)
        self.assertRedirects(self._post(self.pedro, hora='08:45:00'), self.url)

    def test_las_marcaciones_del_terminal_no_se_anulan(self):
        ev = AccessEvent.objects.get(employee_no='8458387')
        resp = self.client.post(f'/reportes/ingresos-manuales/{ev.pk}/anular/', {'reason': 'intento'})
        self.assertEqual(resp.status_code, 404)
        ev.refresh_from_db()
        self.assertEqual(ev.status, AccessEvent.Status.OK)


class AuditPageTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            email='admin@test.cl', password='clave-de-prueba', first_name='Jose', last_name='Admin',
            role=Role.objects.get(code='admin'))
        self.station = Station.objects.create(name='Casino', device_id=1000)

    def test_sesiones_y_filtros(self):
        self.client.post('/login/', {'email': 'admin@test.cl', 'password': 'mala'})
        resp = self.client.post('/login/', {'email': 'admin@test.cl', 'password': 'clave-de-prueba'})
        self.assertEqual(resp.status_code, 302)
        self.client.get('/logout/')
        actions = list(AuditLog.objects.order_by('id').values_list('action', flat=True))
        self.assertEqual(actions, ['sesion.fallida', 'sesion.inicio', 'sesion.cierre'])
        self.assertIn('Jose Admin', AuditLog.objects.get(action='sesion.inicio').summary)
        failed = AuditLog.objects.get(action='sesion.fallida')
        self.assertEqual(failed.level, AuditLog.Level.WARNING)
        self.assertNotIn('mala', failed.summary)
        self.assertNotIn('mala', str(failed.data))

        self.client.force_login(self.admin)
        resp = self.client.get('/bitacora/')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Intento de inicio de sesión fallido')
        resp = self.client.get('/bitacora/?cat=terminal')
        self.assertNotContains(resp, 'Intento de inicio de sesión fallido')
        resp = self.client.get('/bitacora/?level=warning&q=fallido')
        self.assertContains(resp, 'Intento de inicio de sesión fallido')

    def test_cambios_de_configuracion_quedan_anotados(self):
        self.client.force_login(self.admin)
        self.client.post(f'/estaciones/{self.station.pk}/modo-pruebas/', {'test_mode': '1'})
        self.client.post(f'/estaciones/{self.station.pk}/turnos/', {'shift_overtime_minutes': '20'})
        entry = AuditLog.objects.get(action='config.modo_pruebas')
        self.assertEqual((entry.user, entry.station, entry.level), (self.admin, self.station, 'warning'))
        self.assertIn('10 → 20', AuditLog.objects.get(action='config.prorroga').summary)
