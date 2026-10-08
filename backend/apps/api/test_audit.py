"""
Bitácora alimentada por el terminal: turnos abiertos y cerrados tal como los informa,
reinicios (con la última señal de vida) y regreso tras un silencio.
"""
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from backend.apps.core.models import AuditLog, Station, StationAPIKey


class TerminalAuditTests(TestCase):
    def setUp(self):
        self.station = Station.objects.create(name='Casino 1', device_id=1000)
        _, key = StationAPIKey.objects.create_key(name='test', station=self.station)
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f'Api-Key {key}')

    def _sync(self, payload):
        resp = self.client.post('/api/sync/', payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def _shift(self, **extra):
        return {'remote_id': 1, 'uid': 'des-1', 'name': 'Desayuno', 'started_at': '2026-10-07T06:01:36',
                'auto': False, 'schedule_uid': 'sch-des', 'service_date': '2026-10-07', **extra}

    def test_apertura_y_cierre_a_su_hora_real(self):
        self._sync({'shifts': [self._shift()]})
        opened = AuditLog.objects.get(action='turno.abierto')
        self.assertEqual(timezone.localtime(opened.at).strftime('%H:%M:%S'), '06:01:36')
        self.assertIn('desde la pantalla del terminal', opened.summary)
        self.assertEqual(opened.station, self.station)
        self.assertIsNotNone(opened.shift)
        self.assertEqual(opened.actor, 'Terminal')

        # mientras siga abierto, reenviarlo no agrega nada
        self._sync({'shifts': [self._shift()]})
        self.assertEqual(AuditLog.objects.filter(category='turno').count(), 1)

        # se cierra como interrumpido: aviso, a la hora de cierre informada
        self._sync({'shifts': [self._shift(ended_at='2026-10-07T09:01:54', end_reason='interrumpido')]})
        closed = AuditLog.objects.get(action='turno.cerrado')
        self.assertEqual(timezone.localtime(closed.at).strftime('%H:%M:%S'), '09:01:54')
        self.assertEqual(closed.level, AuditLog.Level.WARNING)
        self.assertIn('Interrumpido', closed.summary)
        self._sync({'shifts': [self._shift(ended_at='2026-10-07T09:01:54', end_reason='interrumpido')]})
        self.assertEqual(AuditLog.objects.filter(action='turno.cerrado').count(), 1)

    def test_turno_que_llega_ya_cerrado_anota_ambos_hechos(self):
        self._sync({'shifts': [self._shift(auto=True, ended_at='2026-10-07T09:00:00', end_reason='horario')]})
        self.assertEqual(AuditLog.objects.filter(action='turno.abierto').count(), 1)
        closed = AuditLog.objects.get(action='turno.cerrado')
        self.assertEqual(closed.level, AuditLog.Level.INFO)
        self.assertIn('por horario', AuditLog.objects.get(action='turno.abierto').summary)

    def test_reinicio_del_terminal_con_ultima_senal_de_vida(self):
        incident = {'kind': 'restart', 'uid': 'inc-1', 'at': '2026-10-07T09:01:50',
                    'last_alive_at': '2026-10-07T07:41:10'}
        self._sync({'incidents': [incident]})
        entry = AuditLog.objects.get(action='terminal.reinicio')
        self.assertEqual(entry.level, AuditLog.Level.WARNING)
        self.assertEqual(timezone.localtime(entry.at).strftime('%H:%M:%S'), '09:01:50')
        self.assertIn('07:41:10', entry.summary)
        self.assertIn('1 h 20 min', entry.summary)
        self.assertEqual(entry.data['uid'], 'inc-1')
        # idempotente: el terminal lo reenvía hasta que el servidor responde
        self._sync({'incidents': [incident]})
        self.assertEqual(AuditLog.objects.filter(action='terminal.reinicio').count(), 1)

    def test_regreso_tras_silencio(self):
        self._sync({})
        self.assertEqual(AuditLog.objects.filter(action='terminal.primera_sync').count(), 1)
        self._sync({})
        self.assertFalse(AuditLog.objects.filter(action='terminal.sync_reanudada').exists())

        Station.objects.filter(pk=self.station.pk).update(last_sync_at=timezone.now() - timedelta(hours=2))
        self._sync({})
        entry = AuditLog.objects.get(action='terminal.sync_reanudada')
        self.assertEqual(entry.level, AuditLog.Level.WARNING)
        self.assertIn('2 h 0 min', entry.summary)

    def test_el_terminal_no_puede_mandar_el_estado_anulado(self):
        resp = self.client.post('/api/sync/', {'events': [{
            'remote_id': 1, 'uid': 'e1', 'employee_no': '1', 'event_time': '2026-10-07T07:00:00',
            'status': 'Anulado'}]}, format='json')
        self.assertEqual(resp.status_code, 400)
