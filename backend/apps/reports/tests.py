"""
Pruebas del informe de colaciones con turnos de ingreso manual (la once que se deja
preparada fuera del casino: la cocinera registra la cantidad, nadie marca).
"""
from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Role, Shift, Station, Visit, VisitorCard
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
        # marcación fuera de turno histórica (el kiosco ya no las registra): no suma a nada
        AccessEvent.objects.create(
            station=self.station, uid='ev9', remote_id=9, employee_no='9', person_name='Paseante',
            company='PPE', event_time=_at(self.day, 16), status=AccessEvent.Status.SIN_TURNO,
        )

    def _report(self, **kw):
        return build_report(_at(self.day, 0), _at(self.day + timedelta(days=1), 0), **kw)

    def test_manual_count_adds_to_totals(self):
        data = self._report()
        self.assertEqual(data.manuales, 25)
        self.assertEqual(data.servidas, 2 + 25)   # en turno + manual
        self.assertEqual(data.por_dia, [(self.day, 27)])
        self.assertEqual(data.personas_unicas, 2)

    def test_manual_count_by_shift_and_company(self):
        data = self._report()
        self.assertEqual(data.meal_types, ['Almuerzo', 'Once'])

        rows = {r.shift.name: r for r in data.por_turno}
        alm, once = rows['Almuerzo'], rows['Once']
        self.assertEqual((alm.en_turno, alm.manuales, alm.total), (2, 0, 2))
        self.assertEqual((once.en_turno, once.manuales, once.total), (0, 25, 25))

        por_empresa = {emp: (total, comp) for emp, total, comp in data.por_empresa}
        self.assertEqual(por_empresa[MANUAL_COMPANY], (25, [0, 25]))
        self.assertEqual(sum(t for t, _ in por_empresa.values()), data.servidas)

        # el detalle por persona solo tiene colaciones servidas (sin la de fuera de turno)
        self.assertEqual(sorted(n for n, *_ in data.por_persona), ['Ana', 'Luis'])

    def test_shift_filter_keeps_only_that_shift(self):
        data = self._report(shift_name='Once')
        self.assertEqual((data.servidas, data.manuales), (25, 25))
        data = self._report(shift_name='Almuerzo')
        self.assertEqual((data.servidas, data.manuales), (2, 0))

    def test_pdf_and_excel_render_with_manual_entry(self):
        data = self._report()
        self.assertTrue(build_pdf(data, 'Informe').startswith(b'%PDF'))
        self.assertTrue(build_excel(data, 'Informe').startswith(b'PK'))

    def test_shift_list_and_detail_pages(self):
        """Detalle de colaciones: listado por apertura y, por turno, quién marcó y a qué hora."""
        user = get_user_model().objects.create_user(
            email='g@g.cl', password='x', first_name='G', last_name='G', role=Role.objects.get(code='gerente'))
        self.client.force_login(user)
        Shift.objects.filter(pk=self.almuerzo.pk).update(auto=True, end_reason=Shift.EndReason.MANUAL)
        AccessEvent.objects.create(
            station=self.station, uid='ev5', remote_id=5, shift=self.almuerzo, employee_no='1',
            person_name='Ana', company='PPE', event_time=_at(self.day, 12, 30),
            status=AccessEvent.Status.DUPLICADO)

        resp = self.client.get(f'/reportes/turnos/?from={self.day:%Y-%m-%d}&to={self.day:%Y-%m-%d}')
        self.assertEqual(resp.status_code, 200)
        rows = {s.name: s for s in resp.context['shifts']}
        self.assertEqual((rows['Almuerzo'].n_ok, rows['Almuerzo'].n_dup, rows['Almuerzo'].n_denied), (2, 1, 0))
        self.assertContains(resp, 'automático')   # inicio por horario
        self.assertContains(resp, 'ingreso manual')

        resp = self.client.get(f'/reportes/turnos/{self.almuerzo.pk}/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([e.person_name for e in resp.context['events']], ['Ana', 'Luis', 'Ana'])
        self.assertEqual((resp.context['n_ok'], resp.context['n_personas'], resp.context['n_dup']), (2, 2, 1))
        self.assertContains(resp, '12:11:00')     # hora de la marcación al segundo
        self.assertContains(resp, '12:00:00')     # inicio del turno al segundo

        # turno de ingreso manual: sin marcaciones, muestra la cantidad
        self.assertContains(self.client.get(f'/reportes/turnos/{self.once.pk}/'), 'ingreso manual')

        resp = self.client.get(f'/reportes/turnos/{self.almuerzo.pk}/excel/')
        self.assertTrue(resp.content.startswith(b'PK'))
        self.assertEqual(self.client.get('/reportes/turnos/999999/').status_code, 404)

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
            email='g@g.cl', password='x', first_name='G', last_name='G', role=Role.objects.get(code='gerente'))
        self.client.force_login(user)
        resp = self.client.get('/')
        self.assertEqual(resp.context['total_mes'], 2 + 30)
        self.assertEqual(list(resp.context['por_estacion']), [{'station__name': 'Casino', 'total': 32}])

    def test_report_page_shows_manual_column(self):
        user = get_user_model().objects.create_user(
            email='g@g.cl', password='x', first_name='G', last_name='G', role=Role.objects.get(code='gerente'))
        self.client.force_login(user)
        resp = self.client.get(f'/reportes/?from={self.day:%Y-%m-%d}&to={self.day:%Y-%m-%d}')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Ingreso manual')
        self.assertContains(resp, MANUAL_COMPANY)


class UnauthorizedDetailTests(TestCase):
    """La reportería lista quiénes quedaron NO AUTORIZADOS y por qué."""

    def setUp(self):
        self.station = Station.objects.create(name='Casino', device_id=1000)
        self.day = timezone.localdate() - timedelta(days=1)
        self.almuerzo = Shift.objects.create(
            station=self.station, uid='alm', remote_id=1, name='Almuerzo',
            started_at=_at(self.day, 12), ended_at=_at(self.day, 14), service_date=self.day,
        )
        AccessEvent.objects.create(
            station=self.station, uid='ok', remote_id=1, shift=self.almuerzo,
            employee_no='1', person_name='Ana', company='PPE',
            event_time=_at(self.day, 12, 5), status=AccessEvent.Status.OK,
        )
        AccessEvent.objects.create(
            station=self.station, uid='na2', remote_id=2, shift=self.almuerzo,
            employee_no='77', person_name='Pedro', company='EXTERNA',
            event_time=_at(self.day, 12, 40), status=AccessEvent.Status.NO_AUTORIZADO,
            detail='Empresa EXTERNA no autorizada en el turno Almuerzo',
        )
        AccessEvent.objects.create(
            station=self.station, uid='na1', remote_id=3, shift=self.almuerzo,
            employee_no='V', card_no='0099', is_visitor=True,
            event_time=_at(self.day, 12, 20), status=AccessEvent.Status.NO_AUTORIZADO,
            detail='Tarjeta de visita no registrada',
        )

    def _report(self, **kw):
        return build_report(_at(self.day, 0), _at(self.day + timedelta(days=1), 0), **kw)

    def test_detail_lists_unauthorized_in_order(self):
        data = self._report()
        self.assertEqual(data.no_autorizados, 2)
        self.assertEqual([e.uid for e in data.no_autorizados_detalle], ['na1', 'na2'])
        self.assertEqual(self._report(shift_name='Once').no_autorizados_detalle, [])

    def test_page_pdf_and_excel_show_detail(self):
        user = get_user_model().objects.create_user(
            email='g@g.cl', password='x', first_name='G', last_name='G', role=Role.objects.get(code='gerente'))
        self.client.force_login(user)
        resp = self.client.get(f'/reportes/?from={self.day:%Y-%m-%d}&to={self.day:%Y-%m-%d}')
        self.assertContains(resp, 'id="no-autorizados"')
        self.assertContains(resp, 'Pedro')
        self.assertContains(resp, 'Empresa EXTERNA no autorizada en el turno Almuerzo')
        self.assertContains(resp, '0099')

        data = self._report()
        self.assertTrue(build_pdf(data, 'Informe').startswith(b'%PDF'))
        self.assertTrue(build_excel(data, 'Informe').startswith(b'PK'))


class VisitReportTests(TestCase):
    """La sección «Visitas» del informe: a quién se entregó cada tarjeta y qué retiró."""

    def setUp(self):
        self.station = Station.objects.create(name='Casino', device_id=1000)
        self.day = timezone.localdate() - timedelta(days=1)
        self.almuerzo = Shift.objects.create(
            station=self.station, uid='alm', remote_id=1, name='Almuerzo',
            started_at=_at(self.day, 12), ended_at=_at(self.day, 14), service_date=self.day,
        )
        VisitorCard.objects.create(station=self.station, card_no='002', label='Visita 02')
        VisitorCard.objects.create(station=self.station, card_no='003', label='Visita 03')
        self.visit = Visit.objects.create(
            station=self.station, card_no='002', card_label='Visita 02',
            visitor_name='Juanito Pérez', host_name='Nicolás Muñoz', host_company='PTJ',
            delivered_by_name='Portería', delivered_at=_at(self.day, 11),
        )
        # registrada pero no retiró colación: NO debe figurar ni contarse en el informe
        Visit.objects.create(
            station=self.station, card_no='003', card_label='Visita 03', visitor_name='Solo Pasó',
            host_company='ACME', delivered_at=_at(self.day, 9), returned_at=_at(self.day, 9, 30),
        )
        for i, (card, hh, mm) in enumerate([('002', 12, 10), ('002', 12, 40), ('003', 12, 20)], start=1):
            AccessEvent.objects.create(
                station=self.station, uid=f'v{i}', remote_id=i, shift=self.almuerzo,
                employee_no=f'card:{card}', person_name=f'Visita {card[-2:]}', company='Visita',
                card_no=card, is_visitor=True, event_time=_at(self.day, hh, mm),
                status=AccessEvent.Status.OK if i != 2 else AccessEvent.Status.DUPLICADO,
            )

    def _report(self, **kw):
        return build_report(_at(self.day, 0), _at(self.day + timedelta(days=1), 0), **kw)

    def test_visits_with_their_meals_and_unregistered_cards(self):
        data = self._report()
        self.assertEqual(data.visitas, 2)
        self.assertEqual(data.visitas_sin_registro, 1)   # la 003 se usó después de devuelta

        rows = {(r.visitor_name, r.card_no): r for r in data.visitas_detalle}
        self.assertEqual(rows[('Juanito Pérez', '002')].colaciones, 1)   # la repetida no cuenta
        self.assertEqual(rows[('Juanito Pérez', '002')].host_display, 'Nicolás Muñoz (PTJ)')
        self.assertNotIn(('Solo Pasó', '003'), rows)   # no fue al casino: no se cuenta
        sin = rows[('(sin registro de visita)', '003')]
        self.assertFalse(sin.registrada)
        self.assertEqual((sin.colaciones, sin.card_label), (1, 'Visita 03'))
        # orden cronológico por entrega (o primera colación si no hay registro)
        self.assertEqual([r.card_no for r in data.visitas_detalle], ['002', '003'])
        self.assertTrue(all(r.colaciones for r in data.visitas_detalle))

        # en el detalle por persona la visita registrada aparece con su nombre
        nombres = [n for n, *_ in data.por_persona]
        self.assertIn('Juanito Pérez · Visita 02', nombres)
        self.assertIn('Visita 03', nombres)

    def test_shift_filter_drops_visits_without_meals(self):
        data = self._report(shift_name='Almuerzo')
        self.assertEqual(sorted(r.card_no for r in data.visitas_detalle), ['002', '003'])
        self.assertTrue(all(r.colaciones for r in data.visitas_detalle))

    def test_pdf_and_excel_render_the_visits_section(self):
        data = self._report()
        pdf = build_pdf(data, 'Informe de prueba')
        self.assertTrue(pdf.startswith(b'%PDF'))
        xlsx = build_excel(data, 'Informe de prueba')
        self.assertTrue(xlsx.startswith(b'PK'))
