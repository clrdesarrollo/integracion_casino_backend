"""
Pruebas del informe de colaciones con turnos de ingreso manual (la once que se deja
preparada fuera del casino: la cocinera registra la cantidad, nadie marca).
"""
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Shift, Station
from backend.apps.reports.excel import build_excel
from backend.apps.reports.pdf import build_pdf
from backend.apps.reports.service import MANUAL_COMPANY, build_report


def _at(day, hh, mm=0):
    return timezone.make_aware(datetime.combine(day, datetime.min.time()).replace(hour=hh, minute=mm))


class ManualEntryReportTests(TestCase):
    def setUp(self):
        self.station = Station.objects.create(name='Casino', device_id=1000)
        self.day = timezone.localdate() - timedelta(days=1)

        self.almuerzo = Shift.objects.create(
            station=self.station, uid='alm', remote_id=1, name='Almuerzo',
            started_at=_at(self.day, 12), ended_at=_at(self.day, 14), service_date=self.day,
        )
        # registro de ingreso manual: nace cerrado, sin marcaciones
        self.once = Shift.objects.create(
            station=self.station, uid='once', remote_id=2, name='Once',
            started_at=_at(self.day, 16, 5), ended_at=_at(self.day, 16, 5), service_date=self.day,
            end_reason=Shift.EndReason.MANUAL_ENTRY, manual_count=25,
        )
        for i, (who, company) in enumerate([('Ana', 'PPE'), ('Luis', 'CLROBOTICS')], start=1):
            AccessEvent.objects.create(
                station=self.station, uid=f'ev{i}', remote_id=i, shift=self.almuerzo,
                employee_no=str(i), person_name=who, company=company,
                event_time=_at(self.day, 12, 10 + i), status=AccessEvent.Status.OK,
            )
        # marcación fuera de turno justo antes del registro manual: NO debe atribuirse a él
        # (cae en el último turno terminado antes de la marca, el almuerzo: regla de siempre)
        AccessEvent.objects.create(
            station=self.station, uid='ev9', remote_id=9, employee_no='9', person_name='Paseante',
            company='PPE', event_time=_at(self.day, 16), status=AccessEvent.Status.SIN_TURNO,
        )

    def _report(self, **kw):
        return build_report(_at(self.day, 0), _at(self.day + timedelta(days=1), 0),
                            grace_minutes=15, **kw)

    def test_manual_count_adds_to_totals(self):
        data = self._report()
        self.assertEqual(data.manuales, 25)
        self.assertEqual(data.servidas, 2 + 1 + 25)   # en turno + asociada + manual
        self.assertEqual(data.por_dia, [(self.day, 28)])
        self.assertEqual(data.sin_asociar, 0)
        self.assertEqual(data.personas_unicas, 3)

    def test_manual_count_by_shift_and_company(self):
        data = self._report()
        self.assertEqual(data.meal_types, ['Almuerzo', 'Once'])

        rows = {r.shift.name: r for r in data.por_turno}
        alm, once = rows['Almuerzo'], rows['Once']
        self.assertEqual((alm.en_turno, alm.asociadas, alm.manuales, alm.total), (2, 1, 0, 3))
        # el registro manual no recibe marcaciones fuera de turno: solo su cantidad
        self.assertEqual((once.en_turno, once.asociadas, once.manuales, once.total), (0, 0, 25, 25))

        por_empresa = {emp: (total, comp) for emp, total, comp in data.por_empresa}
        self.assertEqual(por_empresa[MANUAL_COMPANY], (25, [0, 25]))
        self.assertEqual(sum(t for t, _ in por_empresa.values()), data.servidas)

        # el detalle por persona solo tiene marcaciones personales
        self.assertEqual(sorted(n for n, *_ in data.por_persona), ['Ana', 'Luis', 'Paseante'])

    def test_shift_filter_keeps_only_that_shift(self):
        data = self._report(shift_name='Once')
        self.assertEqual((data.servidas, data.manuales), (25, 25))
        data = self._report(shift_name='Almuerzo')
        self.assertEqual((data.servidas, data.manuales), (3, 0))

    def test_pdf_and_excel_render_with_manual_entry(self):
        data = self._report()
        self.assertTrue(build_pdf(data, 'Informe').startswith(b'%PDF'))
        self.assertTrue(build_excel(data, 'Informe').startswith(b'PK'))

    def test_dashboard_month_total_includes_manual(self):
        today = timezone.localdate()
        Shift.objects.all().delete()   # AccessEvent.shift queda en NULL; el evento sigue contando
        AccessEvent.objects.update(event_time=_at(today, 0, 1))
        Shift.objects.create(
            station=self.station, uid='once-hoy', remote_id=3, name='Once',
            started_at=_at(today, 0, 2), ended_at=_at(today, 0, 2), service_date=today,
            end_reason=Shift.EndReason.MANUAL_ENTRY, manual_count=30,
        )
        user = get_user_model().objects.create_user(
            email='g@g.cl', password='x', first_name='G', last_name='G', role='gerente')
        self.client.force_login(user)
        resp = self.client.get('/')
        self.assertEqual(resp.context['total_mes'], 2 + 30)
        self.assertEqual(list(resp.context['por_estacion']), [{'station__name': 'Casino', 'total': 32}])

    def test_report_page_shows_manual_column(self):
        user = get_user_model().objects.create_user(
            email='g@g.cl', password='x', first_name='G', last_name='G', role='gerente')
        self.client.force_login(user)
        resp = self.client.get(f'/reportes/?from={self.day:%Y-%m-%d}&to={self.day:%Y-%m-%d}')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ingreso manual')
        self.assertContains(resp, MANUAL_COMPANY)
