"""
Pruebas de la ingesta con configuración compartida (turnos/empresas/tarjetas de visita):
gana la edición más reciente entre el terminal y el backoffice.
"""
import base64
from datetime import timedelta, timezone as dt_tz

from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from backend.apps.core.models import (
    AccessEvent, Person, Shift, ShiftSchedule, Station, StationAPIKey, VisitorCard,
)


def _stamp(dt):
    dt = timezone.localtime(dt, dt_tz.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%S.') + f'{dt.microsecond // 1000:03d}Z'


class ConfigSyncTests(TestCase):
    def setUp(self):
        self.station = Station.objects.create(name='Casino 1', device_id=1000)
        _, key = StationAPIKey.objects.create_key(name='test', station=self.station)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Api-Key {key}')

    def _sync(self, payload):
        resp = self.client.post('/api/sync/', payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _config(self, stamp, name='Almuerzo', companies=None, cards=None):
        return {
            'updated_at': stamp,
            'schedules': [{
                'remote_id': 1, 'name': name, 'start_min': 720, 'end_min': 840,
                'enabled': True, 'all_companies': companies is None,
                'allow_visitors': True, 'companies': companies or [],
            }],
            'visitor_cards': cards if cards is not None else [
                {'card_no': '0012345678', 'label': 'Visita 01', 'enabled': True,
                 'created_at': '2026-08-17T10:00:00'},
            ],
        }

    def test_terminal_config_is_stored_when_newer(self):
        now = timezone.now()
        body = self._sync({'config': self._config(_stamp(now), companies=['Casino Central', ''])})
        self.assertNotIn('config', body)  # el servidor no tenía nada más nuevo
        self.station.refresh_from_db()
        self.assertIsNotNone(self.station.config_updated_at)
        sched = ShiftSchedule.objects.get(station=self.station)
        self.assertEqual(sched.name, 'Almuerzo')
        self.assertFalse(sched.all_companies)
        self.assertEqual(sorted(sched.company_names), ['', 'Casino Central'])
        self.assertEqual(VisitorCard.objects.get(station=self.station).card_no, '0012345678')

    def test_server_config_is_returned_when_newer(self):
        old = timezone.now() - timedelta(minutes=10)
        self._sync({'config': self._config(_stamp(old))})
        # edición en el backoffice (más reciente)
        sched = ShiftSchedule.objects.get(station=self.station)
        sched.name = 'Almuerzo web'
        sched.save()
        self.station.touch_config()

        # el terminal vuelve a sincronizar con su marca antigua: recibe la config del servidor
        body = self._sync({'config': self._config(_stamp(old))})
        self.assertIn('config', body)
        self.assertEqual(body['config']['schedules'][0]['name'], 'Almuerzo web')
        self.assertEqual(body['config']['updated_at'], _stamp(self.station.config_updated_at))
        # y el servidor no fue pisado
        self.assertEqual(ShiftSchedule.objects.get(station=self.station).name, 'Almuerzo web')

        # el terminal aplica y reenvía con la marca del servidor: ya no hay nada que devolver
        body = self._sync({'config': self._config(body['config']['updated_at'], name='Almuerzo web')})
        self.assertNotIn('config', body)

    def test_terminal_without_stamp_adopts_server_config(self):
        ShiftSchedule.objects.create(station=self.station, name='Cena', start_min=1200, end_min=1320)
        self.station.touch_config()
        body = self._sync({'config': {'updated_at': '', 'schedules': [], 'visitor_cards': []}})
        self.assertIn('config', body)
        self.assertEqual(body['config']['schedules'][0]['name'], 'Cena')

    def test_visitor_event_fields(self):
        body = self._sync({'events': [{
            'remote_id': 5, 'employee_no': 'card:0012345678', 'person_name': 'Visita 01',
            'company': 'Visita', 'verify_method': 'Tarjeta RFID', 'card_no': '0012345678',
            'event_time': '2026-08-17T12:30:00', 'status': 'Duplicado',
            'is_visitor': True, 'detail': 'Tarjeta repetida',
        }]})
        self.assertEqual(body['result']['events']['created'], 1)
        ev = AccessEvent.objects.get(station=self.station, remote_id=5)
        self.assertTrue(ev.is_visitor)
        self.assertEqual(ev.detail, 'Tarjeta repetida')


class ShiftIdentityTests(TestCase):
    """Identidad de cada apertura de turno: uid estable, causa de cierre y reapertura."""

    def setUp(self):
        self.station = Station.objects.create(name='Casino 1', device_id=1000)
        _, key = StationAPIKey.objects.create_key(name='test', station=self.station)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Api-Key {key}')

    def _sync(self, payload):
        resp = self.client.post('/api/sync/', payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _shift(self, uid, remote_id=1, **extra):
        return {
            'remote_id': remote_id, 'uid': uid, 'name': 'Almuerzo',
            'started_at': '2026-08-17T12:00:00', 'auto': True,
            'schedule_uid': 'sched-1', 'service_date': '2026-08-17',
            **extra,
        }

    def test_shift_identified_by_uid_not_remote_id(self):
        """Recrear la base del terminal reinicia los remote_id: el uid evita pisar historia."""
        self._sync({'shifts': [self._shift('uid-a', remote_id=1)]})
        # base recreada: otro turno reusa el remote_id 1 con uid propio
        self._sync({'shifts': [self._shift('uid-b', remote_id=1, name='Cena')]})

        self.assertEqual(Shift.objects.filter(station=self.station).count(), 2)
        self.assertEqual(Shift.objects.get(uid='uid-a').name, 'Almuerzo')
        self.assertEqual(Shift.objects.get(uid='uid-b').name, 'Cena')

    def test_shift_end_reason_and_reopening(self):
        self._sync({'shifts': [
            self._shift('uid-a', remote_id=1,
                        ended_at='2026-08-17T14:00:00', end_reason='reemplazado'),
            self._shift('uid-b', remote_id=2, reopened_from_uid='uid-a'),
        ]})
        original = Shift.objects.get(uid='uid-a')
        reopened = Shift.objects.get(uid='uid-b')
        self.assertEqual(original.end_reason, Shift.EndReason.REPLACED)
        self.assertFalse(original.is_reopening)
        self.assertTrue(reopened.is_reopening)
        self.assertEqual(reopened.reopened_from_uid, original.uid)

    def test_legacy_row_adopts_uid_instead_of_duplicating(self):
        """Un turno respaldado antes de los uid lo adopta al reenviarse (no se duplica)."""
        legacy = Shift.objects.create(
            station=self.station, remote_id=7, name='Almuerzo',
            # misma hora de inicio que el payload: es la que identifica al turno.
            # El terminal envía hora local, así que se interpreta igual que el serializer.
            started_at=timezone.make_aware(parse_datetime('2026-08-17T12:00:00')),
        )
        self.assertEqual(legacy.uid, '')

        self._sync({'shifts': [self._shift('uid-nuevo', remote_id=7)]})

        self.assertEqual(Shift.objects.filter(station=self.station).count(), 1)
        legacy.refresh_from_db()
        self.assertEqual(legacy.uid, 'uid-nuevo')
        self.assertEqual(legacy.schedule_uid, 'sched-1')

    def test_legacy_row_is_not_overwritten_when_local_db_was_recreated(self):
        """
        Si se recrea casino.db, los remote_id vuelven a 1. Una marcación nueva con ese
        remote_id NO debe pisar la histórica que quedó con uid='' (se perdería historia).
        """
        historica = AccessEvent.objects.create(
            station=self.station, remote_id=1, uid='', employee_no='1001',
            person_name='Ana', event_time=timezone.now() - timedelta(days=30),
            status=AccessEvent.Status.OK,
        )
        self._sync({'events': [{
            'remote_id': 1, 'uid': 'ev-tras-recrear', 'employee_no': '2002',
            'person_name': 'Luis', 'company': '', 'verify_method': 'Rostro', 'card_no': '',
            'event_time': '2026-08-20T12:00:00', 'status': 'Ok',
        }]})

        self.assertEqual(AccessEvent.objects.filter(station=self.station).count(), 2)
        historica.refresh_from_db()
        self.assertEqual(historica.person_name, 'Ana')   # intacta
        self.assertEqual(historica.uid, '')
        self.assertEqual(AccessEvent.objects.get(uid='ev-tras-recrear').person_name, 'Luis')

    def test_event_links_shift_by_uid(self):
        self._sync({
            'shifts': [self._shift('uid-a', remote_id=1)],
            'events': [{
                'remote_id': 1, 'uid': 'ev-1', 'shift_uid': 'uid-a', 'shift_remote_id': 1,
                'employee_no': '1001', 'person_name': 'Ana', 'company': 'X',
                'verify_method': 'Rostro', 'card_no': '',
                'event_time': '2026-08-17T12:30:00', 'status': 'Ok',
            }],
        })
        ev = AccessEvent.objects.get(uid='ev-1')
        self.assertEqual(ev.shift.uid, 'uid-a')

    def test_visitor_photo_is_stored_and_kept_on_resend(self):
        photo = base64.b64encode(b'\xff\xd8\xff-jpeg-falso').decode()
        event = {
            'remote_id': 9, 'uid': 'ev-foto', 'employee_no': 'card:001',
            'person_name': 'Visita 01', 'company': 'Visita',
            'verify_method': 'Tarjeta RFID', 'card_no': '001',
            'event_time': '2026-08-17T12:30:00', 'status': 'Ok', 'is_visitor': True,
            'photo_b64': photo,
        }
        self._sync({'events': [event]})
        ev = AccessEvent.objects.get(uid='ev-foto')
        self.assertTrue(ev.has_photo)
        self.assertEqual(bytes(ev.photo), b'\xff\xd8\xff-jpeg-falso')

        # el mismo evento reenviado sin foto no borra la ya respaldada
        event.pop('photo_b64')
        self._sync({'events': [event]})
        ev.refresh_from_db()
        self.assertEqual(bytes(ev.photo), b'\xff\xd8\xff-jpeg-falso')

    def test_corrupt_photo_is_discarded_not_rejected(self):
        self._sync({'events': [{
            'remote_id': 10, 'uid': 'ev-mala', 'employee_no': 'card:002',
            'person_name': 'Visita 02', 'company': 'Visita',
            'verify_method': 'Tarjeta RFID', 'card_no': '002',
            'event_time': '2026-08-17T12:31:00', 'status': 'Ok', 'is_visitor': True,
            'photo_b64': 'esto-no-es-base64-!!',
        }]})
        ev = AccessEvent.objects.get(uid='ev-mala')
        self.assertFalse(ev.has_photo)


class ShiftOvertimeConfigTests(TestCase):
    """La prórroga de cierre viaja en la configuración compartida, en ambos sentidos."""

    def setUp(self):
        self.station = Station.objects.create(name='Casino 1', device_id=1000)
        _, key = StationAPIKey.objects.create_key(name='test', station=self.station)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Api-Key {key}')

    def _sync(self, payload):
        resp = self.client.post('/api/sync/', payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def test_terminal_overtime_wins_when_newer(self):
        self._sync({'config': {
            'updated_at': _stamp(timezone.now()),
            'shift_overtime_minutes': 25,
            'schedules': [], 'visitor_cards': [],
        }})
        self.station.refresh_from_db()
        self.assertEqual(self.station.shift_overtime_minutes, 25)

    def test_server_overtime_is_returned_to_terminal(self):
        old = timezone.now() - timedelta(minutes=10)
        self._sync({'config': {'updated_at': _stamp(old), 'shift_overtime_minutes': 10,
                               'schedules': [], 'visitor_cards': []}})
        self.station.shift_overtime_minutes = 45
        self.station.save(update_fields=['shift_overtime_minutes'])
        self.station.touch_config()

        body = self._sync({'config': {'updated_at': _stamp(old), 'shift_overtime_minutes': 10,
                                      'schedules': [], 'visitor_cards': []}})
        self.assertEqual(body['config']['shift_overtime_minutes'], 45)

    def test_old_terminal_without_overtime_keeps_server_value(self):
        self.station.shift_overtime_minutes = 30
        self.station.save(update_fields=['shift_overtime_minutes'])
        self._sync({'config': {'updated_at': _stamp(timezone.now()),
                               'schedules': [], 'visitor_cards': []}})
        self.station.refresh_from_db()
        self.assertEqual(self.station.shift_overtime_minutes, 30)

    def test_days_mask_survives_sync(self):
        """Los días en que se sirve el turno no deben perderse al sincronizar."""
        self._sync({'config': {
            'updated_at': _stamp(timezone.now()),
            'schedules': [{'remote_id': 1, 'uid': 'sc-once', 'name': 'Once',
                           'start_min': 840, 'end_min': 960, 'enabled': True,
                           'days_mask': 0b0011111,   # lunes a viernes
                           'all_companies': True, 'allow_visitors': True, 'companies': []}],
            'visitor_cards': [],
        }})
        sched = ShiftSchedule.objects.get(station=self.station)
        self.assertEqual(sched.days_mask, 0b0011111)
        self.assertEqual(sched.days_text, 'Lun a Vie')

    def test_schedule_uid_survives_sync(self):
        """El uid del turno programado se conserva: liga cada apertura a su definición."""
        self._sync({'config': {
            'updated_at': _stamp(timezone.now()),
            'schedules': [{'remote_id': 1, 'uid': 'sched-fijo', 'name': 'Almuerzo',
                           'start_min': 720, 'end_min': 840, 'enabled': True,
                           'all_companies': True, 'allow_visitors': True, 'companies': []}],
            'visitor_cards': [],
        }})
        self.assertEqual(ShiftSchedule.objects.get(station=self.station).uid, 'sched-fijo')

    def test_is_lunch_survives_sync_and_falls_back_to_name(self):
        """La marca de almuerzo viaja en la config; sin ella (terminal antiguo) se decide por el nombre."""
        self._sync({'config': {
            'updated_at': _stamp(timezone.now()),
            'schedules': [
                {'remote_id': 1, 'uid': 'sc-once', 'name': 'Once', 'start_min': 960,
                 'end_min': 1080, 'enabled': True, 'is_lunch': True,
                 'all_companies': True, 'allow_visitors': True, 'companies': []},
                {'remote_id': 2, 'uid': 'sc-alm', 'name': 'ALMUERZO', 'start_min': 720,
                 'end_min': 840, 'enabled': True,
                 'all_companies': True, 'allow_visitors': True, 'companies': []},
                {'remote_id': 3, 'uid': 'sc-cena', 'name': 'Cena', 'start_min': 1200,
                 'end_min': 1320, 'enabled': True, 'is_lunch': False,
                 'all_companies': True, 'allow_visitors': True, 'companies': []},
            ],
            'visitor_cards': [],
        }})
        by_uid = {s.uid: s for s in ShiftSchedule.objects.filter(station=self.station)}
        self.assertTrue(by_uid['sc-once'].is_lunch)     # explícito
        self.assertTrue(by_uid['sc-alm'].is_lunch)      # sin dato → por nombre
        self.assertFalse(by_uid['sc-cena'].is_lunch)

        # y el servidor la devuelve cuando su copia es más nueva
        self.station.config_updated_at = timezone.now() + timedelta(seconds=5)
        self.station.save(update_fields=['config_updated_at'])
        body = self._sync({'config': {'updated_at': _stamp(timezone.now()),
                                      'schedules': [], 'visitor_cards': []}})
        lunch_flags = {s['uid']: s['is_lunch'] for s in body['config']['schedules']}
        self.assertEqual(lunch_flags, {'sc-once': True, 'sc-alm': True, 'sc-cena': False})

    def test_person_meal_policy_survives_sync(self):
        """La colación asignada (campo Colacion de HikCentral) se respalda por persona."""
        self._sync({'persons': [
            {'employee_no': '1-9', 'name': 'Sin', 'meal_policy': 0},
            {'employee_no': '2-7', 'name': 'Almuerzo', 'meal_policy': 1},
            {'employee_no': '3-5', 'name': 'Todos', 'meal_policy': 2},
            {'employee_no': '4-3', 'name': 'Vacío', 'meal_policy': None},
            {'employee_no': '5-1', 'name': 'Terminal antiguo'},
        ]})
        policies = dict(Person.objects.filter(station=self.station)
                        .values_list('employee_no', 'meal_policy'))
        self.assertEqual(policies, {'1-9': 0, '2-7': 1, '3-5': 2, '4-3': None, '5-1': None})
        self.assertEqual(Person.objects.get(employee_no='2-7').meal_policy_text, 'Solo almuerzo')
        self.assertEqual(Person.objects.get(employee_no='4-3').meal_policy_text, 'Sin definir')

        # un valor fuera de 0..2 se rechaza completo (el terminal nunca lo manda)
        resp = self.client.post('/api/sync/', {'persons': [
            {'employee_no': '9-9', 'meal_policy': 7},
        ]}, format='json')
        self.assertEqual(resp.status_code, 400)

        # al volver a sincronizar con el campo vaciado en HikCentral, se limpia aquí también
        self._sync({'persons': [{'employee_no': '1-9', 'name': 'Sin', 'meal_policy': None}]})
        self.assertIsNone(Person.objects.get(employee_no='1-9').meal_policy)


class VisitorEventsViewTests(TestCase):
    """Visor de colaciones de visitas (pagos adicionales)."""

    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        # personal del casino: es el rol con el acceso más acotado que igual ve los tickets
        self.user = User.objects.create_user(
            email='v@v.cl', password='x', first_name='V', last_name='V', role='casino',
        )
        self.station = Station.objects.create(name='Casino 1', device_id=1000)
        self.client.force_login(self.user)

        self.visita = AccessEvent.objects.create(
            station=self.station, remote_id=1, uid='ev-1', employee_no='card:001',
            person_name='Visita 01', card_no='001', event_time=timezone.now(),
            status=AccessEvent.Status.OK, is_visitor=True, photo=b'\xff\xd8\xff-foto',
        )
        self.personal = AccessEvent.objects.create(
            station=self.station, remote_id=2, uid='ev-2', employee_no='1001',
            person_name='Ana', event_time=timezone.now(), status=AccessEvent.Status.OK,
        )

    def test_lists_only_visitor_events(self):
        resp = self.client.get('/visitas/')
        self.assertContains(resp, 'Visita 01')
        self.assertNotContains(resp, '>Ana<')

    def test_photo_is_served_for_visitor_event(self):
        resp = self.client.get(f'/visitas/{self.visita.pk}/foto/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'image/jpeg')
        self.assertEqual(resp.content, b'\xff\xd8\xff-foto')

    def test_photo_of_non_visitor_event_is_not_exposed(self):
        resp = self.client.get(f'/visitas/{self.personal.pk}/foto/')
        self.assertEqual(resp.status_code, 404)

    def test_filter_by_status(self):
        AccessEvent.objects.create(
            station=self.station, remote_id=3, uid='ev-3', employee_no='card:002',
            person_name='Visita 02', card_no='002', event_time=timezone.now(),
            status=AccessEvent.Status.DUPLICADO, is_visitor=True,
        )
        resp = self.client.get('/visitas/?estado=Duplicado')
        self.assertContains(resp, 'Visita 02')
        self.assertNotContains(resp, 'Visita 01')

    def test_login_required(self):
        self.client.logout()
        resp = self.client.get('/visitas/')
        self.assertEqual(resp.status_code, 302)


class ConfigWebTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        self.admin = User.objects.create_user(email='a@a.cl', password='x', first_name='A', last_name='B', role='admin')
        self.station = Station.objects.create(name='Casino 1', device_id=1000)
        self.client.force_login(self.admin)

    def test_schedule_crud_touches_config(self):
        resp = self.client.get('/turnos/')
        self.assertEqual(resp.status_code, 302)  # una sola estación → directo a sus turnos
        resp = self.client.get(f'/estaciones/{self.station.pk}/turnos/nuevo/')
        self.assertContains(resp, 'Nuevo turno')
        resp = self.client.get(f'/estaciones/{self.station.pk}/turnos/')
        self.assertContains(resp, 'Sin turnos programados')
        resp = self.client.post(f'/estaciones/{self.station.pk}/turnos/nuevo/', {
            'name': 'Almuerzo', 'start': '12:00', 'end': '14:00', 'enabled': 'on',
            'allow_visitors': 'on', 'other_companies': 'Casino Central, Aseo SpA',
        })
        self.assertEqual(resp.status_code, 302, resp.content[:500])
        sched = ShiftSchedule.objects.get(station=self.station)
        self.assertFalse(sched.all_companies)
        self.assertEqual(sorted(sched.company_names), ['Aseo SpA', 'Casino Central'])
        self.station.refresh_from_db()
        self.assertIsNotNone(self.station.config_updated_at)
        resp = self.client.get(f'/estaciones/{self.station.pk}/turnos/')
        self.assertContains(resp, 'Almuerzo')
        resp = self.client.get(f'/estaciones/{self.station.pk}/turnos/{sched.pk}/')
        self.assertContains(resp, 'Casino Central')

    def test_overtime_is_saved_and_marks_config_as_edited(self):
        """Al guardar la prórroga, la marca de configuración avanza: el terminal la adoptará."""
        resp = self.client.get(f'/estaciones/{self.station.pk}/turnos/')
        self.assertContains(resp, 'Prórroga')

        resp = self.client.post(f'/estaciones/{self.station.pk}/turnos/', {
            'shift_overtime_minutes': '35',
        })
        self.assertEqual(resp.status_code, 302, resp.content[:400])
        self.station.refresh_from_db()
        self.assertEqual(self.station.shift_overtime_minutes, 35)
        self.assertIsNotNone(self.station.config_updated_at)

    def test_overtime_out_of_range_is_rejected(self):
        self.station.shift_overtime_minutes = 10
        self.station.save(update_fields=['shift_overtime_minutes'])
        resp = self.client.post(f'/estaciones/{self.station.pk}/turnos/', {
            'shift_overtime_minutes': '999',
        })
        self.assertEqual(resp.status_code, 200)  # se vuelve a mostrar el formulario con el error
        self.station.refresh_from_db()
        self.assertEqual(self.station.shift_overtime_minutes, 10)

    def test_schedule_form_saves_is_lunch(self):
        resp = self.client.post(f'/estaciones/{self.station.pk}/turnos/nuevo/', {
            'name': 'Colación mediodía', 'start': '12:00', 'end': '14:00', 'enabled': 'on',
            'allow_visitors': 'on', 'all_companies': 'on', 'is_lunch': 'on',
        })
        self.assertEqual(resp.status_code, 302, resp.content[:500])
        sched = ShiftSchedule.objects.get(station=self.station)
        self.assertTrue(sched.is_lunch)
        resp = self.client.get(f'/estaciones/{self.station.pk}/turnos/')
        self.assertContains(resp, 'Almuerzo')   # insignia en la lista

        # desmarcar
        resp = self.client.post(f'/estaciones/{self.station.pk}/turnos/{sched.pk}/', {
            'name': 'Colación mediodía', 'start': '12:00', 'end': '14:00', 'enabled': 'on',
            'allow_visitors': 'on', 'all_companies': 'on',
        })
        self.assertEqual(resp.status_code, 302)
        sched.refresh_from_db()
        self.assertFalse(sched.is_lunch)

    def test_person_list_shows_meal_policy(self):
        Person.objects.create(station=self.station, employee_no='10585109K', name='Luis Perez',
                              company='Casino', meal_policy=1)
        Person.objects.create(station=self.station, employee_no='13063883', name='Gonzalo Palma',
                              company='Casino', meal_policy=0)
        Person.objects.create(station=self.station, employee_no='21457536', name='Juan Puebla',
                              company='Casino')
        Person.objects.create(station=self.station, employee_no='card:1', name='Visita',
                              user_type='visitor')
        resp = self.client.get(f'/estaciones/{self.station.pk}/personas/')
        self.assertContains(resp, 'Luis Perez')
        self.assertContains(resp, 'Solo almuerzo')
        self.assertContains(resp, 'Sin colación')
        self.assertContains(resp, 'Sin definir')
        self.assertNotContains(resp, '>Visita<')   # las visitas van por tarjeta, no por HikCentral
        self.assertEqual(resp.context['counts'], {'total': 3, 'none': 1, '0': 1, '1': 1, '2': 0})

        resp = self.client.get(f'/estaciones/{self.station.pk}/personas/?colacion=1')
        self.assertContains(resp, 'Luis Perez')
        self.assertNotContains(resp, 'Gonzalo Palma')
        resp = self.client.get(f'/estaciones/{self.station.pk}/personas/?q=palma')
        self.assertContains(resp, 'Gonzalo Palma')
        self.assertNotContains(resp, 'Luis Perez')

    def test_visitor_card_add_and_list(self):
        resp = self.client.post(f'/estaciones/{self.station.pk}/tarjetas/', {
            'card_no': ' 00 1234 ', 'label': '', 'enabled': 'on',
        })
        self.assertEqual(resp.status_code, 302)
        card = VisitorCard.objects.get(station=self.station)
        self.assertEqual(card.card_no, '001234')
        self.assertEqual(card.label, 'Visita 01')
        resp = self.client.get(f'/estaciones/{self.station.pk}/tarjetas/')
        self.assertContains(resp, '001234')
