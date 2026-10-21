"""
Colaciones programadas (entrega en mano): la regla se aplica sola al cierre del turno, con
la hora de inicio; se omite en feriados, excepciones y si la persona marcó; registra igual
pasada la tolerancia sin cierre y se anula sola si después llega una marcación del terminal.
"""
from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from backend.apps.core.models import (
    AccessEvent, AuditLog, Holiday, Person, Role, Shift, ShiftSchedule, StandingMeal,
    StandingMealException, StandingMealRun, Station, StationAPIKey,
)
from backend.apps.reports import standing
from backend.apps.reports.excel import build_excel, build_shift_excel
from backend.apps.reports.mailing import run_scheduler_tick
from backend.apps.reports.pdf import build_pdf
from backend.apps.reports.service import build_report
from backend.apps.webapp.permissions import DENIED_MESSAGE

Status = StandingMealRun.Status


def _at(day, hh, mm=0, ss=0):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm, second=ss))


class StandingMealBase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            email='admin@test.cl', password='x', first_name='Jose', last_name='Admin',
            role=Role.objects.get(code='admin'))
        self.station = Station.objects.create(name='Casino', device_id=1000)   # prórroga: 10 min
        # Once 16:00–17:00, todos los días
        self.schedule = ShiftSchedule.objects.create(
            station=self.station, uid='sch-once', name='Once', start_min=960, end_min=1020)
        self.alejandro = Person.objects.create(
            station=self.station, employee_no='10121385', name='Alejandro San Martín',
            company='PPE', meal_policy=Person.MealPolicy.ALL_SHIFTS)
        today = timezone.localdate()
        # el lunes de la semana pasada: dentro de los 14 días que revisa el programador
        self.monday = today - timedelta(days=today.weekday() + 7)
        self.tuesday = self.monday + timedelta(days=1)
        self.friday = self.monday + timedelta(days=4)
        self.meal = StandingMeal.objects.create(
            station=self.station, employee_no='10121385', person_name='Alejandro San Martín',
            company='PPE', schedule_uid='sch-once', schedule_name='Once',
            valid_from=self.monday, grace_minutes=60,
            note='Cubre turno de 8 a 20 lejos del casino; la once se la lleva el personal',
            created_by=self.admin, created_by_name='Jose Admin')
        self.client.force_login(self.admin)

    def _shift(self, day, closed=True, uid=None, remote_id=None, reopened_from=''):
        n = Shift.objects.count() + 1
        return Shift.objects.create(
            station=self.station, uid=uid or f'once-{day:%Y%m%d}-{n}', remote_id=remote_id or n,
            name='Once', started_at=_at(day, 16, 0, 5),
            ended_at=_at(day, 17, 0, 2) if closed else None,
            service_date=day, schedule_uid='sch-once', auto=True,
            end_reason=Shift.EndReason.EXPIRED if closed else '', reopened_from_uid=reopened_from)

    def _terminal_mark(self, shift, day, hh=16, mm=20):
        return AccessEvent.objects.create(
            station=self.station, uid=f'ev-{day:%Y%m%d}-{hh}{mm}', remote_id=100 + hh * 60 + mm,
            shift=shift, employee_no='10121385', person_name='Alejandro San Martín', company='PPE',
            verify_method='Rostro', event_time=_at(day, hh, mm), status=AccessEvent.Status.OK)

    def _run(self, day=None):
        return StandingMealRun.objects.get(meal=self.meal, service_date=day or self.monday)

    def _scheduled(self):
        return AccessEvent.objects.filter(origin=AccessEvent.Origin.SCHEDULED)


class StandingMealLogicTests(StandingMealBase):
    def test_registra_al_cierre_con_la_hora_de_inicio_y_sin_duplicar(self):
        shift = self._shift(self.monday)
        self.assertEqual(standing.process(_at(self.monday, 17, 30)), 1)

        ev = self._scheduled().get()
        self.assertEqual((ev.status, ev.origin), (AccessEvent.Status.OK, AccessEvent.Origin.SCHEDULED))
        self.assertTrue(ev.is_scheduled and ev.is_backoffice and not ev.is_manual)
        self.assertEqual((ev.shift, ev.event_time), (shift, shift.started_at))
        self.assertEqual((ev.person_name, ev.company, ev.employee_no), ('Alejandro San Martín', 'PPE', '10121385'))
        self.assertEqual((ev.entered_by, ev.entered_by_name), (None, 'Sistema'))
        self.assertEqual(ev.standing_meal, self.meal)
        self.assertIn('Programada', ev.verify_method)
        self.assertIn('Cubre turno', ev.entry_reason)
        self.assertIn('Colación programada', ev.manual_note)

        run = self._run()
        self.assertEqual((run.status, run.shift, run.event), (Status.REGISTERED, shift, ev))
        self.assertIn('al cierre del turno', run.detail)
        entry = AuditLog.objects.get(action='colacion.programada')
        self.assertEqual((entry.event, entry.shift, entry.level), (ev, shift, 'info'))
        self.assertIn('Alejandro San Martín', entry.summary)

        # otro ciclo, y el disparador de la ingesta: nada se duplica
        self.assertEqual(standing.process(_at(self.monday, 18, 30)), 0)
        standing.on_shifts_reported([shift.pk])
        self.assertEqual(self._scheduled().count(), 1)

    def test_sale_en_el_informe_y_en_el_detalle_como_programada(self):
        shift = self._shift(self.monday)
        standing.process(_at(self.monday, 17, 30))
        data = build_report(_at(self.monday, 0), _at(self.monday + timedelta(days=1), 0))
        self.assertEqual((data.servidas, data.programadas, data.backoffice), (1, 1, 0))
        self.assertEqual([e.origin_text for e in data.backoffice_detalle], ['colación programada'])
        self.assertIn(('PPE', 1, [1]), data.por_empresa)
        self.assertEqual(data.por_turno[0].en_turno, 1)
        self.assertTrue(build_pdf(data, 'Informe').startswith(b'%PDF'))
        self.assertTrue(build_excel(data, 'Informe').startswith(b'PK'))
        self.assertTrue(build_shift_excel(shift, list(shift.events.all())).startswith(b'PK'))

        resp = self.client.get(f'/reportes/?from={self.monday:%Y-%m-%d}&to={self.monday:%Y-%m-%d}')
        self.assertContains(resp, 'programada')
        resp = self.client.get(f'/reportes/turnos/{shift.pk}/')
        self.assertContains(resp, 'Programada')
        self.assertEqual((resp.context['n_ok'], resp.context['n_scheduled'], resp.context['n_manual']), (1, 1, 0))
        resp = self.client.get(f'/reportes/turnos/?from={self.monday:%Y-%m-%d}&to={self.monday:%Y-%m-%d}')
        self.assertContains(resp, '1 programada')
        resp = self.client.get(f'/reportes/ingresos-manuales/?from={self.monday:%Y-%m-%d}&to={self.monday:%Y-%m-%d}')
        self.assertContains(resp, 'Alejandro San Martín')
        self.assertContains(resp, 'programada')

    def test_se_omite_si_ese_dia_marco_en_el_terminal(self):
        shift = self._shift(self.monday)
        self._terminal_mark(shift, self.monday)
        self.assertEqual(standing.process(_at(self.monday, 17, 30)), 0)
        self.assertFalse(self._scheduled().exists())
        run = self._run()
        self.assertEqual(run.status, Status.SKIPPED)
        self.assertIn('Ya tenía colación válida a las 16:20:00', run.detail)
        self.assertEqual(AuditLog.objects.filter(action='colacion.programada_omitida').count(), 1)

    def test_feriados_excepciones_y_dias_que_no_corresponden(self):
        Holiday.objects.create(date=self.monday, name='Feriado de prueba')
        StandingMealException.objects.create(meal=self.meal, date_from=self.tuesday, date_to=self.tuesday,
                                             reason='Vacaciones')
        for day in (self.monday, self.tuesday, self.friday):
            self._shift(day)
        standing.process(_at(self.friday, 18, 0))
        self.assertFalse(self._scheduled().exists())
        self.assertIn('Feriado de prueba', self._run(self.monday).detail)
        self.assertIn('Vacaciones', self._run(self.tuesday).detail)
        # miércoles y jueves sin turno y ya pasada la tolerancia: «sin turno», con aviso;
        # el viernes la regla no rige (lunes a jueves): ni siquiera se anota
        wednesday, thursday = self.monday + timedelta(days=2), self.monday + timedelta(days=3)
        self.assertEqual({r.service_date: r.status for r in self.meal.runs.all()}, {
            self.monday: Status.SKIPPED, self.tuesday: Status.SKIPPED,
            wednesday: Status.NO_SHIFT, thursday: Status.NO_SHIFT,
        })
        self.assertEqual(AuditLog.objects.filter(action='colacion.programada_sin_turno').count(), 2)

    def test_turno_abierto_espera_el_cierre_y_registra_pasada_la_tolerancia(self):
        shift = self._shift(self.monday, closed=False)
        standing.process(_at(self.monday, 17, 30))      # término 17:00 + prórroga 10 + tolerancia 60 = 18:10
        self.assertEqual(self._run().status, Status.PENDING)
        self.assertIn('sigue abierto', self._run().detail)
        self.assertFalse(self._scheduled().exists())

        self.assertEqual(standing.process(_at(self.monday, 18, 15)), 1)
        ev = self._scheduled().get()
        self.assertEqual((ev.shift, ev.event_time), (shift, shift.started_at))
        run = self._run()
        self.assertEqual(run.status, Status.REGISTERED)
        self.assertIn('sin cierre', run.detail)
        self.assertEqual(AuditLog.objects.get(action='colacion.programada').level, 'warning')

        # después marca en el terminal en ese mismo servicio: la programada se anula sola
        mark = self._terminal_mark(shift, self.monday, 18, 40)
        standing.on_terminal_events([mark.pk], now=_at(self.monday, 18, 41))
        ev.refresh_from_db()
        self.assertTrue(ev.is_annulled)
        self.assertEqual(ev.annulled_by_name, 'Sistema')
        self.assertIn('18:40:00', ev.annul_reason)
        self.assertEqual(self._run().status, Status.ANNULLED)
        self.assertEqual(AuditLog.objects.filter(action='colacion.programada_anulada').count(), 1)
        # ya anulada, otro ciclo no la vuelve a registrar (el día quedó resuelto)
        self.assertEqual(standing.process(_at(self.monday, 19, 0)), 0)
        data = build_report(_at(self.monday, 0), _at(self.monday + timedelta(days=1), 0))
        self.assertEqual((data.servidas, data.programadas), (1, 0))

    def test_sin_turno_queda_pendiente_y_se_registra_cuando_el_turno_llega(self):
        standing.process(_at(self.monday, 17, 30))
        self.assertEqual(self._run().status, Status.PENDING)
        standing.process(_at(self.monday, 18, 30))
        self.assertEqual(self._run().status, Status.NO_SHIFT)
        entry = AuditLog.objects.get(action='colacion.programada_sin_turno')
        self.assertEqual(entry.level, 'warning')
        # el aviso no se repite en cada ciclo
        standing.process(_at(self.monday, 19, 30))
        self.assertEqual(AuditLog.objects.filter(action='colacion.programada_sin_turno').count(), 1)
        # el terminal informa el turno al día siguiente: se registra entonces
        shift = self._shift(self.monday)
        self.assertEqual(standing.process(_at(self.tuesday, 9, 0)), 1)
        self.assertEqual((self._run().status, self._run().shift), (Status.REGISTERED, shift))

    def test_reapertura_no_duplica_y_una_marcacion_en_ella_anula_la_programada(self):
        original = self._shift(self.monday, uid='once-orig')
        standing.process(_at(self.monday, 17, 30))
        reopened = self._shift(self.monday, closed=False, uid='once-re', reopened_from='once-orig')
        standing.on_shifts_reported([reopened.pk])
        self.assertEqual(self._scheduled().count(), 1)
        mark = self._terminal_mark(reopened, self.monday, 17, 20)
        standing.on_terminal_events([mark.pk])
        self.assertTrue(self._scheduled().get().is_annulled)
        self.assertEqual(original.events.filter(status=AccessEvent.Status.OK).count(), 0)

    def test_regla_inactiva_o_fuera_de_vigencia_no_registra(self):
        self._shift(self.monday)
        self.meal.active = False
        self.meal.save()
        standing.process(_at(self.monday, 17, 30))
        self.assertFalse(StandingMealRun.objects.exists())
        self.meal.active = True
        self.meal.valid_to = self.monday - timedelta(days=1)
        self.meal.save()
        standing.process(_at(self.monday, 17, 30))
        self.assertFalse(StandingMealRun.objects.exists())

    def test_persona_fuera_de_la_ficha_se_registra_con_el_nombre_guardado(self):
        self._shift(self.monday)
        self.alejandro.delete()
        standing.process(_at(self.monday, 17, 30))
        ev = self._scheduled().get()
        self.assertEqual((ev.person_name, ev.company), ('Alejandro San Martín', 'PPE'))
        self.assertIn('ya no está en la ficha', self._run().detail)
        resp = self.client.get('/reportes/colaciones-programadas/')
        self.assertContains(resp, 'ya no está en la ficha')

    def test_el_programador_de_correos_aplica_las_reglas(self):
        self._shift(self.monday)
        run_scheduler_tick(_at(self.monday, 17, 30))
        self.assertEqual(self._scheduled().count(), 1)

    def test_anular_desde_ingresos_manuales_actualiza_el_historial(self):
        self._shift(self.monday)
        standing.process(_at(self.monday, 17, 30))
        ev = self._scheduled().get()
        resp = self.client.post(f'/reportes/ingresos-manuales/{ev.pk}/anular/',
                                {'reason': 'Ese día no vino nadie', 'next': '/reportes/colaciones-programadas/'})
        self.assertRedirects(resp, '/reportes/colaciones-programadas/')
        ev.refresh_from_db()
        self.assertTrue(ev.is_annulled)
        self.assertEqual(ev.annulled_by_name, 'Jose Admin')
        run = self._run()
        self.assertEqual(run.status, Status.ANNULLED)
        self.assertIn('Jose Admin', run.detail)
        self.assertIn('programada ANULADA', AuditLog.objects.get(action='colacion.anulada').summary)


class StandingMealApiTests(StandingMealBase):
    def setUp(self):
        super().setUp()
        _, key = StationAPIKey.objects.create_key(name='test', station=self.station)
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f'Api-Key {key}')

    def _sync(self, payload, now=None):
        """Sincroniza como el terminal; `now` fija el reloj con que se aplican las reglas."""
        with patch('django.utils.timezone.now', return_value=now or _at(self.monday, 17, 30)):
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.api.post('/api/sync/', payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)

    def _shift_payload(self, closed, **extra):
        data = {'remote_id': 5, 'uid': 'once-api', 'name': 'Once', 'auto': True,
                'started_at': _at(self.monday, 16, 0, 5).isoformat(),
                'schedule_uid': 'sch-once', 'service_date': self.monday.isoformat()}
        if closed:
            data.update(ended_at=_at(self.monday, 17, 0, 2).isoformat(), end_reason='expirado')
        data.update(extra)
        return data

    def test_el_cierre_informado_por_el_terminal_registra_la_colacion(self):
        self._sync({'shifts': [self._shift_payload(closed=False)]}, now=_at(self.monday, 16, 10))
        self.assertEqual(self._run().status, Status.PENDING)
        self.assertFalse(self._scheduled().exists())

        self._sync({'shifts': [self._shift_payload(closed=True)]}, now=_at(self.monday, 17, 5))
        ev = self._scheduled().get()
        self.assertEqual(ev.shift.uid, 'once-api')
        self.assertEqual(self._run().status, Status.REGISTERED)
        # el terminal reenvía el mismo turno cerrado: nada cambia
        self._sync({'shifts': [self._shift_payload(closed=True)]})
        self.assertEqual(self._scheduled().count(), 1)

    def test_una_marcacion_del_terminal_anula_la_programada_del_servicio(self):
        self._sync({'shifts': [self._shift_payload(closed=True)]})
        ev = self._scheduled().get()
        self._sync({'events': [{
            'remote_id': 77, 'uid': 'ev-api', 'shift_uid': 'once-api', 'employee_no': '10121385',
            'person_name': 'Alejandro San Martín', 'company': 'PPE', 'verify_method': 'Rostro',
            'event_time': _at(self.monday, 16, 45).isoformat(), 'status': 'Ok',
        }]})
        ev.refresh_from_db()
        self.assertTrue(ev.is_annulled)
        self.assertEqual(self._run().status, Status.ANNULLED)
        self.assertEqual(self.station.events.filter(status=AccessEvent.Status.OK).count(), 1)


class StandingMealViewTests(StandingMealBase):
    def setUp(self):
        super().setUp()
        self.gerente = get_user_model().objects.create_user(
            email='g@test.cl', password='x', first_name='G', last_name='G',
            role=Role.objects.get(code='gerente'))
        self.url = '/reportes/colaciones-programadas/'

    def _form(self, **extra):
        data = {'station': self.station.pk, 'employee_no': '10121385', 'schedule_uid': 'sch-once',
                'weekdays': ['0', '1', '2', '3'], 'valid_from': self.monday.isoformat(),
                'grace_minutes': 45, 'note': 'Recibe la once en terreno, se la lleva el casino',
                'active': 'on'}
        data.update(extra)
        return data

    def test_solo_quien_tiene_el_permiso_entra(self):
        self.client.force_login(self.gerente)
        resp = self.client.get(self.url, follow=True)
        self.assertIn(DENIED_MESSAGE, [str(m) for m in get_messages(resp.wsgi_request)])
        self.assertEqual(self.client.post(f'{self.url}nueva/', self._form()).status_code, 302)
        self.assertEqual(StandingMeal.objects.count(), 1)

    def test_lista_crear_editar_y_pausar(self):
        resp = self.client.get(self.url)
        self.assertContains(resp, 'Alejandro San Martín')
        self.assertContains(resp, 'lunes a jueves')

        rows = self.client.get(f'{self.url}personas/', {'estacion': self.station.pk, 'q': 'san martin'}).json()['results']
        self.assertEqual([r['employee_no'] for r in rows], ['10121385'])

        resp = self.client.post(f'{self.url}nueva/', self._form(weekdays=['4'], note='Los viernes también, en mano'))
        meal = StandingMeal.objects.exclude(pk=self.meal.pk).get()
        self.assertRedirects(resp, f'{self.url}{meal.pk}/')
        self.assertEqual((meal.person_name, meal.company, meal.schedule_name), ('Alejandro San Martín', 'PPE', 'Once'))
        self.assertEqual((meal.days_mask, meal.grace_minutes, meal.created_by_name), (1 << 4, 45, 'Jose Admin'))
        self.assertTrue(meal.applies_on(self.friday))
        self.assertFalse(meal.applies_on(self.monday))

        # sin persona de la ficha, sin días o con vigencia al revés: no se guarda
        self.assertEqual(self.client.post(f'{self.url}nueva/', self._form(employee_no='999')).status_code, 200)
        self.assertEqual(self.client.post(f'{self.url}nueva/', self._form(weekdays=[])).status_code, 200)
        self.assertEqual(self.client.post(
            f'{self.url}nueva/', self._form(valid_to=(self.monday - timedelta(days=1)).isoformat())).status_code, 200)
        self.assertEqual(StandingMeal.objects.count(), 2)

        resp = self.client.post(f'{self.url}{meal.pk}/', self._form(weekdays=['0', '4'], grace_minutes=30))
        self.assertRedirects(resp, f'{self.url}{meal.pk}/')
        meal.refresh_from_db()
        self.assertEqual((meal.days_mask, meal.grace_minutes), ((1 << 0) | (1 << 4), 30))

        resp = self.client.post(f'{self.url}{meal.pk}/alternar/')
        self.assertRedirects(resp, self.url)
        meal.refresh_from_db()
        self.assertFalse(meal.active)
        self.assertGreaterEqual(AuditLog.objects.filter(action='config.colacion_programada').count(), 3)

    def test_excepciones_e_historial_en_la_edicion(self):
        shift = self._shift(self.monday)
        standing.process(_at(self.monday, 17, 30))
        resp = self.client.post(f'{self.url}{self.meal.pk}/excepciones/',
                                {'date_from': self.tuesday.isoformat(), 'date_to': self.friday.isoformat(),
                                 'reason': 'Vacaciones'})
        self.assertRedirects(resp, f'{self.url}{self.meal.pk}/')
        exc = self.meal.exceptions.get()
        self.assertTrue(exc.covers(self.tuesday) and exc.covers(self.friday) and not exc.covers(self.monday))

        resp = self.client.get(f'{self.url}{self.meal.pk}/')
        self.assertContains(resp, 'Vacaciones')
        self.assertContains(resp, 'Registrada')
        self.assertContains(resp, f'/reportes/turnos/{shift.pk}/')
        # ya registró colaciones: no se elimina
        self.client.post(f'{self.url}{self.meal.pk}/eliminar/')
        self.assertTrue(StandingMeal.objects.filter(pk=self.meal.pk).exists())

        self.client.post(f'{self.url}{self.meal.pk}/excepciones/{exc.pk}/eliminar/')
        self.assertFalse(self.meal.exceptions.exists())

        # una regla sin colaciones registradas sí se elimina
        other = StandingMeal.objects.create(
            station=self.station, employee_no='10121385', person_name='Alejandro San Martín',
            schedule_uid='sch-once', schedule_name='Once', valid_from=self.monday, note='Prueba de borrado')
        self.assertRedirects(self.client.post(f'{self.url}{other.pk}/eliminar/'), self.url)
        self.assertFalse(StandingMeal.objects.filter(pk=other.pk).exists())

    def test_feriados_a_mano_y_cargados_desde_la_lista_de_chile(self):
        resp = self.client.post('/reportes/feriados/', {'date': '2026-12-08', 'name': 'Inmaculada Concepción', 'year': 2026})
        self.assertRedirects(resp, '/reportes/feriados/?year=2026')
        self.assertEqual(Holiday.objects.count(), 1)
        # repetido: se avisa y no se duplica
        self.client.post('/reportes/feriados/', {'date': '2026-12-08', 'name': 'Otra vez', 'year': 2026})
        self.assertEqual(Holiday.objects.count(), 1)

        resp = self.client.post('/reportes/feriados/cargar-chile/', {'year': 2026})
        self.assertRedirects(resp, '/reportes/feriados/?year=2026')
        self.assertEqual(Holiday.objects.filter(date__year=2026).count(), 16)
        self.assertEqual(Holiday.objects.get(date='2026-12-08').name, 'Inmaculada Concepción')
        self.assertEqual(self.client.post('/reportes/feriados/cargar-chile/', {'year': 1999}).status_code, 302)

        resp = self.client.get('/reportes/feriados/?year=2026')
        self.assertContains(resp, 'Glorias Navales')
        holiday = Holiday.objects.get(date='2026-05-21')
        self.client.post(f'/reportes/feriados/{holiday.pk}/eliminar/')
        self.assertEqual(Holiday.objects.filter(date__year=2026).count(), 15)
        self.assertEqual(AuditLog.objects.filter(action='config.feriado').count(), 3)
