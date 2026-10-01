"""
Matriz de acceso del backoffice.

Cada rol solo entra a lo suyo; lo demás se niega. Estas pruebas recorren TODAS las rutas
para los tres roles, así que una vista nueva sin protección se nota al agregarla a la lista.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from backend.apps.core.models import (
    AccessEvent, Person, Role, Station, Visit, VisitorCard,
)
from backend.apps.webapp.permissions import DENIED_MESSAGE

User = get_user_model()

# Rutas del backoffice agrupadas por la capacidad que exigen.
PANEL = ['/']
TICKETS = ['/monitor/', '/monitor/turnos/', '/visitas/', '/visitas/colaciones/',
           '/visitas/funcionarios/', '/reportes/', '/reportes/pdf/', '/reportes/excel/',
           '/reportes/turnos/']
CONFIG = ['/turnos/', '/estaciones/', '/usuarios/', '/visitas/tarjetas/']


def crear_usuario(email, role):
    """`role` es el código del rol ('admin', 'gerente', 'casino') o un Role."""
    if isinstance(role, str):
        role = Role.objects.get(code=role)
    return User.objects.create_user(
        email=email, password='clave-de-prueba', first_name='N', last_name='N', role=role,
    )


class RolePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = crear_usuario('admin@test.cl', 'admin')
        cls.gerente = crear_usuario('gerente@test.cl', 'gerente')
        cls.casino = crear_usuario('casino@test.cl', 'casino')
        cls.station = Station.objects.create(name='Casino 1', device_id=1000)
        cls.visita = AccessEvent.objects.create(
            station=cls.station, remote_id=1, uid='ev-1', employee_no='card:001',
            person_name='Visita 01', card_no='001', event_time=timezone.now(),
            status=AccessEvent.Status.OK, is_visitor=True, photo=b'\xff\xd8\xff-foto',
        )

    def _entra(self, url):
        """
        True si el usuario llegó al contenido; False si se le negó el paso.

        Se sigue la redirección y se mira el mensaje de denegación, porque no toda
        redirección es un rechazo: «/turnos/» con una sola estación redirige a los
        turnos de esa estación, y eso es acceso concedido.
        """
        resp = self.client.get(url, follow=True)
        self.assertEqual(resp.status_code, 200, f'{url} respondió {resp.status_code}')
        # get_messages y no resp.context: las descargas (PDF, Excel) y el JSON del
        # monitor no renderizan plantilla, así que no traen contexto
        mensajes = [str(m) for m in get_messages(resp.wsgi_request)]
        return DENIED_MESSAGE not in mensajes

    def _comprobar(self, user, permitidas, denegadas):
        self.client.force_login(user)
        for url in permitidas:
            self.assertTrue(self._entra(url), f'{user.role} DEBERÍA entrar a {url}')
        for url in denegadas:
            self.assertFalse(self._entra(url), f'{user.role} NO debería entrar a {url}')

    def test_administrador_entra_a_todo(self):
        self._comprobar(self.admin, PANEL + TICKETS + CONFIG, [])

    def test_gerente_ve_tickets_y_panel_pero_no_configura(self):
        self._comprobar(self.gerente, PANEL + TICKETS, CONFIG)

    def test_casino_solo_ve_tickets(self):
        # el panel no se le niega: es la raíz del sitio y lo lleva a su propia sección
        self._comprobar(self.casino, TICKETS, CONFIG)

    def test_casino_en_la_raiz_aterriza_en_el_monitor(self):
        self.client.force_login(self.casino)
        resp = self.client.get('/')
        self.assertRedirects(resp, reverse('webapp:monitor'))

    def test_gerente_en_la_raiz_ve_el_panel(self):
        self.client.force_login(self.gerente)
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_anonimo_no_entra_a_ninguna_ruta(self):
        for url in PANEL + TICKETS + CONFIG:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, f'{url} debería exigir sesión')
            self.assertIn('/login/', resp['Location'], f'{url} debería mandar al login')

    def test_foto_de_visita_exige_permiso_de_tickets(self):
        url = f'/visitas/{self.visita.pk}/foto/'
        self.client.force_login(self.casino)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.logout()
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_gerente_no_puede_crear_ni_editar_usuarios(self):
        """La denegación cubre POST, no solo la vista de lectura."""
        self.client.force_login(self.gerente)
        antes = User.objects.count()
        resp = self.client.post('/usuarios/nuevo/', {
            'email': 'colado@test.cl', 'first_name': 'X', 'last_name': 'Y',
            'role': Role.objects.get(code='admin').pk, 'is_active': 'on', 'password': 'algo',
        })
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(User.objects.count(), antes)
        self.assertFalse(User.objects.filter(email='colado@test.cl').exists())

    def test_casino_no_puede_cambiar_la_configuracion_del_kiosco(self):
        self.client.force_login(self.casino)
        resp = self.client.post(f'/estaciones/{self.station.pk}/turnos/', {
            'shift_overtime_minutes': '120',
        })
        self.assertEqual(resp.status_code, 302)
        self.station.refresh_from_db()
        self.assertEqual(self.station.shift_overtime_minutes, 10)

    def test_solo_el_administrador_entra_al_admin_de_django(self):
        for user, esperado in ((self.admin, 200), (self.gerente, 302), (self.casino, 302)):
            self.client.force_login(user)
            resp = self.client.get('/admin/')
            self.assertEqual(resp.status_code, esperado,
                             f'{user.role} en /admin/ esperaba {esperado}')


class InactiveUserTests(TestCase):
    """Desactivar una cuenta corta el acceso de inmediato, con la sesión ya abierta."""

    def test_cuenta_desactivada_pierde_el_acceso(self):
        user = crear_usuario('baja@test.cl', 'admin')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/usuarios/').status_code, 200)

        user.is_active = False
        user.save(update_fields=['is_active'])

        # no hace falta esperar a que caduque la sesión: la siguiente petición ya no pasa
        resp = self.client.get('/usuarios/')
        self.assertEqual(resp.status_code, 302)
        self.assertIn('/login/', resp['Location'])

    def test_cuenta_desactivada_tampoco_ve_las_fotos_de_visitas(self):
        """La denegación cubre también las descargas, no solo las páginas."""
        station = Station.objects.create(name='Casino X', device_id=2000)
        visita = AccessEvent.objects.create(
            station=station, remote_id=1, uid='ev-x', employee_no='card:009',
            person_name='Visita', card_no='009', event_time=timezone.now(),
            status=AccessEvent.Status.OK, is_visitor=True, photo=b'\xff\xd8\xff',
        )
        user = crear_usuario('baja2@test.cl', 'casino')
        self.client.force_login(user)
        self.assertEqual(self.client.get(f'/visitas/{visita.pk}/foto/').status_code, 200)

        user.is_active = False
        user.save(update_fields=['is_active'])
        self.assertEqual(self.client.get(f'/visitas/{visita.pk}/foto/').status_code, 302)


class MonitorWebSocketPermissionTests(TransactionTestCase):
    """
    Por el WebSocket viajan las mismas marcaciones que muestra la página, así que
    debe exigir el mismo permiso: si solo pidiera sesión, sería la puerta de atrás.

    TransactionTestCase (y no TestCase): el consumidor consulta la base dentro del
    bucle async y la transacción envolvente de TestCase no lo permite.
    `serialized_rollback`: el vaciado entre pruebas borraría los roles que siembra la migración.
    """

    serialized_rollback = True

    async def _conectar(self, user):
        from channels.testing import WebsocketCommunicator

        from backend.asgi import application

        communicator = WebsocketCommunicator(application, '/ws/monitor/')
        communicator.scope['user'] = user
        conectado, _ = await communicator.connect()
        await communicator.disconnect()
        return conectado

    async def test_rol_sin_permiso_de_monitor_es_rechazado(self):
        from channels.db import database_sync_to_async
        from django.contrib.auth.models import AnonymousUser

        @database_sync_to_async
        def sin_monitor():
            rol = Role.objects.create(name='Solo reportes', permissions=['reports'])
            return crear_usuario('sinmonitor@test.cl', rol)

        self.assertFalse(await self._conectar(await sin_monitor()),
                         'un rol sin permiso no debe recibir marcaciones en vivo')
        self.assertFalse(await self._conectar(AnonymousUser()),
                         'un anónimo no debe recibir marcaciones en vivo')

    async def test_cuenta_desactivada_es_rechazada(self):
        from channels.db import database_sync_to_async

        inactivo = await database_sync_to_async(crear_usuario)('baja@test.cl', 'casino')
        inactivo.is_active = False
        await database_sync_to_async(inactivo.save)()
        self.assertFalse(await self._conectar(inactivo))

    async def test_personal_del_casino_si_conecta(self):
        from channels.db import database_sync_to_async

        casino = await database_sync_to_async(crear_usuario)('casinows@test.cl', 'casino')
        self.assertTrue(await self._conectar(casino))

    async def test_perder_el_permiso_corta_la_conexion_ya_abierta(self):
        """
        Validar solo al conectar dejaría a quien pierde el permiso recibiendo marcaciones
        mientras no cierre la pestaña.
        """
        from unittest.mock import patch

        from channels.db import database_sync_to_async
        from channels.layers import get_channel_layer
        from channels.testing import WebsocketCommunicator

        from backend.apps.realtime.broadcast import MONITOR_GROUP
        from backend.apps.realtime.consumers import MonitorConsumer
        from backend.asgi import application

        user = await database_sync_to_async(crear_usuario)(
            'degradado@test.cl', 'casino',
        )

        # sin intervalo de gracia: la revalidación ocurre en la primera marcación
        with patch.object(MonitorConsumer, 'REVALIDATE_SECONDS', 0):
            communicator = WebsocketCommunicator(application, '/ws/monitor/')
            communicator.scope['user'] = user
            conectado, _ = await communicator.connect()
            self.assertTrue(conectado)
            await communicator.receive_json_from()   # snapshot inicial

            @database_sync_to_async
            def desactivar():
                user.is_active = False
                user.save(update_fields=['is_active'])

            await desactivar()

            await get_channel_layer().group_send(MONITOR_GROUP, {
                'type': 'monitor.event',
                'event': {'id': 1, 'person_name': 'X', 'status': 'Ok'},
            })

            respuesta = await communicator.receive_output(timeout=3)
            self.assertEqual(respuesta['type'], 'websocket.close',
                             'debía cerrarse la conexión, no entregar la marcación')
            await communicator.disconnect()


class StationWebSocketTests(TransactionTestCase):
    """
    Canal de órdenes hacia el terminal (`/ws/station/`): entra solo con la API key de una
    estación, y lo que se ordena desde el backoffice le llega al terminal conectado.
    """

    serialized_rollback = True

    def _station_with_key(self):
        from backend.apps.core.models import StationAPIKey

        station = Station.objects.create(name='Casino WS', device_id=1000)
        _, key = StationAPIKey.objects.create_key(name='test', station=station)
        return station, key

    def _communicator(self, key=None):
        from channels.testing import WebsocketCommunicator

        from backend.asgi import application

        headers = [(b'authorization', f'Api-Key {key}'.encode())] if key else []
        return WebsocketCommunicator(application, '/ws/station/', headers=headers)

    async def test_sin_api_key_valida_no_conecta(self):
        for key in (None, 'clave.inventada'):
            communicator = self._communicator(key)
            conectado, _ = await communicator.connect()
            self.assertFalse(conectado)

    async def test_la_orden_del_backoffice_llega_al_terminal(self):
        import json

        from channels.db import database_sync_to_async

        station, key = await database_sync_to_async(self._station_with_key)()
        communicator = self._communicator(key)
        conectado, _ = await communicator.connect()
        self.assertTrue(conectado)
        self.assertTrue(await communicator.receive_nothing(timeout=0.2))   # nada pendiente

        await database_sync_to_async(station.request_persons_refresh)()
        self.assertEqual(json.loads(await communicator.receive_from(timeout=3)),
                         {'command': 'sync_persons'})
        await communicator.disconnect()

        # un terminal que se conecta DESPUÉS de la solicitud la recibe al conectar
        communicator = self._communicator(key)
        await communicator.connect()
        self.assertEqual(json.loads(await communicator.receive_from(timeout=3)),
                         {'command': 'sync_persons'})
        await communicator.disconnect()


class LoginRedirectTests(TestCase):
    """El login lleva a cada rol a su sección y no fuera del sitio."""

    @classmethod
    def setUpTestData(cls):
        cls.casino = crear_usuario('casino2@test.cl', 'casino')
        cls.admin = crear_usuario('admin2@test.cl', 'admin')

    def _login(self, email, **extra):
        return self.client.post('/login/', {
            'email': email, 'password': 'clave-de-prueba', **extra,
        })

    def test_casino_aterriza_en_el_monitor(self):
        self.assertRedirects(self._login('casino2@test.cl'), reverse('webapp:monitor'))

    def test_admin_aterriza_en_el_panel(self):
        self.assertRedirects(self._login('admin2@test.cl'), reverse('webapp:dashboard'))

    def test_next_interno_se_respeta(self):
        resp = self._login('admin2@test.cl', next='/estaciones/')
        self.assertRedirects(resp, '/estaciones/')

    def test_next_a_otro_sitio_se_ignora(self):
        """Sin esto, un enlace preparado sacaría al usuario del backoffice tras ingresar."""
        resp = self._login('admin2@test.cl', next='https://sitio-externo.example/roba')
        self.assertRedirects(resp, reverse('webapp:dashboard'))

    def test_next_protocol_relative_se_ignora(self):
        resp = self._login('admin2@test.cl', next='//sitio-externo.example/roba')
        self.assertRedirects(resp, reverse('webapp:dashboard'))


class RoleModelTests(TestCase):
    def test_superusuario_es_admin_aunque_tenga_otro_rol(self):
        u = crear_usuario('super@test.cl', 'casino')
        u.is_superuser = True
        self.assertTrue(u.is_admin)
        self.assertTrue(u.has_cap('dashboard'))

    def test_rol_por_defecto_es_el_mas_restringido(self):
        u = User.objects.create_user(email='nuevo@test.cl', password='x')
        self.assertEqual(u.role.code, 'casino')
        self.assertFalse(u.has_cap('dashboard'))
        self.assertTrue(u.has_cap('monitor'))

    def test_bajar_el_rol_cierra_el_admin_de_django_tambien_con_update_fields(self):
        """
        Un save() parcial es el camino típico de un script de mantención. Si no
        arrastrara is_staff, el ex-administrador conservaría el panel /admin/ de Django,
        que da acceso a toda la configuración.
        """
        user = crear_usuario('exadmin@test.cl', 'admin')
        self.assertTrue(user.is_staff)

        user.role = Role.objects.get(code='gerente')
        user.save(update_fields=['role'])

        user.refresh_from_db()
        self.assertFalse(user.is_staff, 'is_staff quedó en la base pese al cambio de rol')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/admin/').status_code, 302)

    def test_administrador_usa_el_admin_de_django_sin_ser_superusuario(self):
        """Tener is_staff sin permisos por modelo dejaría el panel vacío e inservible."""
        admin = crear_usuario('adminweb@test.cl', 'admin')
        self.assertFalse(admin.is_superuser)
        self.client.force_login(admin)
        resp = self.client.get('/admin/core/station/')
        self.assertEqual(resp.status_code, 200)

    def test_el_formulario_marca_staff_solo_para_administradores(self):
        from backend.apps.webapp.forms import UserForm

        form = UserForm(data={'email': 'g@test.cl', 'first_name': 'G', 'last_name': 'G',
                              'role': Role.objects.get(code='gerente').pk, 'is_active': 'on',
                              'password': 'clave123'})
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertFalse(user.is_staff)


class VisitRegistryTests(TestCase):
    """
    Registro de visitas: a quién se entrega cada tarjeta física, quién la entrega y a quién
    viene a ver; y cómo se atribuyen las colaciones de la tarjeta a la visita vigente.
    """

    def setUp(self):
        self.casino = crear_usuario('porteria@test.cl', 'casino')
        self.admin = crear_usuario('admin@test.cl', 'admin')
        self.station = Station.objects.create(name='Casino 1', device_id=1000)
        self.card = VisitorCard.objects.create(station=self.station, card_no='002', label='Visita 02')
        VisitorCard.objects.create(station=self.station, card_no='009', label='Rota', enabled=False)
        self.host = Person.objects.create(
            station=self.station, employee_no='77', name='Nicolás Muñoz', company='PTJ',
        )
        Person.objects.create(station=self.station, employee_no='78', name='Nicolás Soto', company='ACME')
        self.client.force_login(self.casino)

    def _registrar(self, **extra):
        data = {'station': self.station.pk, 'visitor_name': 'Juanito Pérez', 'card_no': '002',
                'host_company': 'PTJ', 'host_name': 'Nicolás Muñoz',
                'meal_date': timezone.localdate().isoformat()}
        data.update(extra)
        return self.client.post('/visitas/', data)

    def test_el_personal_registra_la_entrega_y_queda_quien_la_hizo(self):
        resp = self._registrar(visitor_document='12.345.678-9')
        self.assertEqual(resp.status_code, 302)
        v = Visit.objects.get()
        self.assertEqual((v.visitor_name, v.card_no, v.card_label), ('Juanito Pérez', '002', 'Visita 02'))
        self.assertEqual(v.delivered_by, self.casino)
        self.assertEqual(v.delivered_by_name, self.casino.full_name)
        self.assertEqual((v.host_company, v.host_name), ('PTJ', 'Nicolás Muñoz'))
        self.assertEqual(v.host_person, self.host)      # ligada a la ficha de HikCentral
        self.assertTrue(v.is_open)

    def test_la_entrega_carga_una_colacion_para_un_dia_y_turno(self):
        from backend.apps.core.models import ShiftSchedule
        hoy = timezone.localdate()
        almuerzo = ShiftSchedule.objects.create(
            station=self.station, name='Almuerzo', start_min=720, end_min=840)
        sin_visitas = ShiftSchedule.objects.create(
            station=self.station, name='Cena', start_min=1200, end_min=1260, allow_visitors=False)

        # día pasado, o un turno que no admite visitas: no se registra
        self.assertEqual(self._registrar(meal_date=(hoy - timedelta(days=1)).isoformat()).status_code, 200)
        self.assertEqual(self._registrar(meal_shift=sin_visitas.uid).status_code, 200)
        self.assertEqual(Visit.objects.count(), 0)

        self.assertEqual(self._registrar(meal_shift=almuerzo.uid).status_code, 302)
        v = Visit.objects.get()
        self.assertEqual((v.meal_date, v.meal_schedule_uid, v.meal_shift_name), (hoy, almuerzo.uid, 'Almuerzo'))
        self.assertEqual((v.meal_state, v.origin), ('pending', 'backoffice'))
        self.assertEqual(Visit.grants_for(self.station), [{
            'uid': v.uid, 'card_no': '002', 'meal_date': hoy.isoformat(),
            'schedule_uid': almuerzo.uid, 'shift_name': 'Almuerzo',
            'visitor_name': 'Juanito Pérez', 'used': False,
        }])

        # devuelta sin usar: la carga deja de valer
        self.client.post(f'/visitas/{v.pk}/devolver/')
        self.assertEqual(Visit.grants_for(self.station), [])
        v.refresh_from_db()
        self.assertEqual(v.meal_state, 'expired')

    def test_funcionario_que_no_esta_en_la_ficha_se_guarda_igual(self):
        self._registrar(host_name='Alguien Nuevo', host_company='Otra')
        v = Visit.objects.get()
        self.assertIsNone(v.host_person)
        self.assertEqual(v.host_display, 'Alguien Nuevo (Otra)')

    def test_hay_que_decir_a_quien_viene_a_ver(self):
        resp = self._registrar(host_company='', host_name='')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'Indica a quién viene a ver')
        self.assertFalse(Visit.objects.exists())

    def test_tarjeta_deshabilitada_o_ajena_no_se_puede_entregar(self):
        for card in ('009', '555'):
            resp = self._registrar(card_no=card)
            self.assertEqual(resp.status_code, 200, card)
        self.assertFalse(Visit.objects.exists())

    def test_reentregar_una_tarjeta_en_uso_cierra_la_visita_anterior(self):
        self._registrar()
        primera = Visit.objects.get()
        self._registrar(visitor_name='Segunda Visita')
        primera.refresh_from_db()
        self.assertFalse(primera.is_open)
        self.assertEqual(primera.returned_by, self.casino)
        self.assertEqual(Visit.objects.open().get().visitor_name, 'Segunda Visita')

    def test_devolver_la_tarjeta(self):
        self._registrar()
        v = Visit.objects.get()
        resp = self.client.post(f'/visitas/{v.pk}/devolver/')
        self.assertEqual(resp.status_code, 302)
        v.refresh_from_db()
        self.assertIsNotNone(v.returned_at)
        self.assertEqual(v.returned_by_name, self.casino.full_name)

    def test_las_colaciones_se_atribuyen_a_la_visita_vigente(self):
        t0 = timezone.now()
        v = Visit.objects.create(station=self.station, card_no='002', visitor_name='Juanito',
                                 delivered_at=t0, returned_at=t0 + timedelta(hours=3))

        def ev(uid, when):
            return AccessEvent.objects.create(
                station=self.station, remote_id=hash(uid) % 10000, uid=uid, employee_no='card:002',
                person_name='Visita 02', card_no='002', event_time=when,
                status=AccessEvent.Status.OK, is_visitor=True,
            )

        antes = ev('a', t0 - timedelta(minutes=5))
        durante = ev('b', t0 + timedelta(hours=1))
        despues = ev('c', t0 + timedelta(hours=4))
        Visit.attach_to_events([antes, durante, despues])
        self.assertIsNone(antes.visit)
        self.assertEqual(durante.visit, v)
        self.assertIsNone(despues.visit)

        # en la pantalla de colaciones aparece el nombre de la visita en esa marcación
        resp = self.client.get('/visitas/colaciones/')
        self.assertContains(resp, 'Juanito')
        self.assertContains(resp, 'sin registro')
        self.assertEqual(resp.context['sin_registro'], 2)

    def test_autocompletado_de_funcionarios_por_empresa(self):
        resp = self.client.get('/visitas/funcionarios/', {'estacion': self.station.pk, 'q': 'nico'})
        nombres = [r['name'] for r in resp.json()['results']]
        self.assertEqual(nombres, ['Nicolás Muñoz', 'Nicolás Soto'])
        resp = self.client.get('/visitas/funcionarios/',
                               {'estacion': self.station.pk, 'q': 'nico', 'empresa': 'ptj'})
        self.assertEqual([r['name'] for r in resp.json()['results']], ['Nicolás Muñoz'])

    def test_solo_el_administrador_borra_un_registro(self):
        self._registrar()
        v = Visit.objects.get()
        self.client.post(f'/visitas/{v.pk}/eliminar/')
        self.assertTrue(Visit.objects.filter(pk=v.pk).exists())
        self.client.force_login(self.admin)
        self.client.post(f'/visitas/{v.pk}/eliminar/')
        self.assertFalse(Visit.objects.filter(pk=v.pk).exists())

    def test_el_inventario_de_tarjetas_sigue_siendo_del_administrador(self):
        resp = self.client.get(f'/estaciones/{self.station.pk}/tarjetas/', follow=True)
        self.assertIn(DENIED_MESSAGE, [str(m) for m in get_messages(resp.wsgi_request)])


class CustomRoleTests(TestCase):
    """Roles creados y editados desde el backoffice: los permisos se aplican tal cual."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = crear_usuario('admin3@test.cl', 'admin')
        cls.station = Station.objects.create(name='Casino 1', device_id=1000)

    def _rol_con(self, *permisos, name='Rol de prueba'):
        rol = Role.objects.create(name=name, permissions=list(permisos))
        return crear_usuario(f'{rol.code}@test.cl', rol)

    def test_rol_nuevo_solo_entra_a_lo_que_se_le_dio(self):
        user = self._rol_con('reports', 'visit_events')
        self.client.force_login(user)
        for url in ('/reportes/', '/visitas/colaciones/'):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
        for url in ('/monitor/', '/visitas/', '/usuarios/', '/roles/', '/turnos/', '/visitas/tarjetas/'):
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 302, f'{url} debería negarse')

    def test_detalle_de_turnos_es_un_permiso_aparte_de_reporteria(self):
        user = self._rol_con('reports')
        self.client.force_login(user)
        resp = self.client.get('/reportes/turnos/', follow=True)
        self.assertIn(DENIED_MESSAGE, [str(m) for m in get_messages(resp.wsgi_request)])
        self.assertNotContains(self.client.get('/reportes/'), 'Detalle de colaciones')

        user.role.permissions = ['reports', 'shifts']
        user.role.save()
        self.assertEqual(self.client.get('/reportes/turnos/').status_code, 200)
        self.assertContains(self.client.get('/reportes/'), 'Detalle de colaciones')

    def test_la_entrada_es_la_primera_seccion_permitida(self):
        user = self._rol_con('reports')
        self.assertEqual(user.home_url_name, 'reports:report')
        self.client.force_login(user)
        self.assertRedirects(self.client.get('/'), reverse('reports:report'))

    def test_rol_sin_permisos_recibe_403_y_no_un_bucle(self):
        user = self._rol_con(name='Vacío')
        self.assertIsNone(user.home_url_name)
        self.client.force_login(user)
        self.assertEqual(self.client.get('/').status_code, 403)
        self.assertEqual(self.client.get('/monitor/').status_code, 403)

    def test_el_menu_solo_muestra_lo_permitido(self):
        user = self._rol_con('reports')
        self.client.force_login(user)
        html = self.client.get('/reportes/').content.decode()
        self.assertIn('Informes', html)
        self.assertNotIn('Monitor en vivo', html)
        self.assertNotIn('>Roles<', html.replace('</i> Roles<', '>Roles<'))

    def test_editar_permisos_de_un_rol_se_aplica_a_sus_usuarios(self):
        user = self._rol_con('monitor')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/monitor/').status_code, 200)
        user.role.permissions = ['reports']
        user.role.save()
        self.assertEqual(self.client.get('/monitor/').status_code, 302)

    def test_admin_crea_y_edita_un_rol_desde_la_web(self):
        self.client.force_login(self.admin)
        resp = self.client.post('/roles/nuevo/', {
            'name': 'Supervisor', 'description': 'Ve todo', 'permissions': ['reports', 'monitor'],
        })
        self.assertEqual(resp.status_code, 302)
        rol = Role.objects.get(name='Supervisor')
        self.assertEqual(rol.permissions, ['monitor', 'reports'])
        self.assertFalse(rol.is_system or rol.is_admin)

        self.client.post(f'/roles/{rol.pk}/editar/', {
            'name': 'Supervisor', 'permissions': ['dashboard'],
        })
        rol.refresh_from_db()
        self.assertEqual(rol.permissions, ['dashboard'])

    def test_nombre_de_rol_repetido_se_rechaza(self):
        self.client.force_login(self.admin)
        resp = self.client.post('/roles/nuevo/', {'name': 'personal del casino', 'permissions': []})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Role.objects.filter(name__iexact='personal del casino').count(), 1)

    def test_el_rol_administrador_no_pierde_permisos(self):
        rol = Role.objects.get(code='admin')
        self.client.force_login(self.admin)
        self.client.post(f'/roles/{rol.pk}/editar/', {'name': 'Administrador', 'permissions': []})
        rol.refresh_from_db()
        self.assertEqual(rol.name, 'Administrador')
        self.assertTrue(rol.allows('users'))
        self.assertTrue(self.admin.has_cap('config'))

    def test_no_se_eliminan_roles_de_sistema_ni_con_usuarios(self):
        self.client.force_login(self.admin)
        sistema = Role.objects.get(code='casino')
        self.client.post(f'/roles/{sistema.pk}/eliminar/')
        self.assertTrue(Role.objects.filter(pk=sistema.pk).exists())

        en_uso = Role.objects.create(name='En uso', permissions=['monitor'])
        crear_usuario('enuso@test.cl', en_uso)
        self.client.post(f'/roles/{en_uso.pk}/eliminar/')
        self.assertTrue(Role.objects.filter(pk=en_uso.pk).exists())

        libre = Role.objects.create(name='Libre')
        self.client.post(f'/roles/{libre.pk}/eliminar/')
        self.assertFalse(Role.objects.filter(pk=libre.pk).exists())

    def test_quien_no_tiene_usuarios_no_gestiona_roles(self):
        user = self._rol_con('config')
        self.client.force_login(user)
        antes = Role.objects.count()
        resp = self.client.post('/roles/nuevo/', {'name': 'Colado', 'permissions': ['users']})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Role.objects.count(), antes)

    def test_rol_propio_no_da_acceso_al_admin_de_django(self):
        user = self._rol_con('users', 'config')
        self.assertFalse(user.is_staff)

    def test_no_se_deja_al_sistema_sin_administrador(self):
        from backend.apps.webapp.forms import UserForm

        casino = Role.objects.get(code='casino')
        form = UserForm(instance=self.admin, data={
            'email': self.admin.email, 'first_name': 'N', 'last_name': 'N',
            'role': casino.pk, 'is_active': 'on',
        })
        self.assertFalse(form.is_valid())

        self.client.force_login(self.admin)
        self.client.post(f'/usuarios/{self.admin.pk}/eliminar/')
        self.assertTrue(User.objects.filter(pk=self.admin.pk).exists())
