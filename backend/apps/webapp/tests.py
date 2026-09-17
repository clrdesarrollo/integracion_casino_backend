"""
Matriz de acceso del backoffice.

Cada rol solo entra a lo suyo; lo demás se niega. Estas pruebas recorren TODAS las rutas
para los tres roles, así que una vista nueva sin protección se nota al agregarla a la lista.
"""
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from backend.apps.core.models import AccessEvent, Station
from backend.apps.webapp.permissions import DENIED_MESSAGE

User = get_user_model()

# Rutas del backoffice agrupadas por la capacidad que exigen.
PANEL = ['/']
TICKETS = ['/monitor/', '/monitor/turnos/', '/visitas/', '/reportes/',
           '/reportes/pdf/', '/reportes/excel/']
CONFIG = ['/turnos/', '/estaciones/', '/usuarios/']


def crear_usuario(email, role):
    return User.objects.create_user(
        email=email, password='clave-de-prueba', first_name='N', last_name='N', role=role,
    )


class RolePermissionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = crear_usuario('admin@test.cl', User.Role.ADMIN)
        cls.gerente = crear_usuario('gerente@test.cl', User.Role.MANAGER)
        cls.casino = crear_usuario('casino@test.cl', User.Role.CASINO)
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
            'role': 'admin', 'is_active': 'on', 'password': 'algo',
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
        user = crear_usuario('baja@test.cl', User.Role.ADMIN)
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
        user = crear_usuario('baja2@test.cl', User.Role.CASINO)
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
    """

    async def _conectar(self, user):
        from channels.testing import WebsocketCommunicator

        from backend.asgi import application

        communicator = WebsocketCommunicator(application, '/ws/monitor/')
        communicator.scope['user'] = user
        conectado, _ = await communicator.connect()
        await communicator.disconnect()
        return conectado

    async def test_rol_sin_permiso_de_tickets_es_rechazado(self):
        from django.contrib.auth.models import AnonymousUser

        # sin guardar: al consumidor le basta el rol para rechazar, no toca la base
        sin_rol = User(email='sinrol@test.cl', role='desconocido')
        self.assertFalse(await self._conectar(sin_rol),
                         'un rol sin permiso no debe recibir marcaciones en vivo')
        self.assertFalse(await self._conectar(AnonymousUser()),
                         'un anónimo no debe recibir marcaciones en vivo')

    async def test_cuenta_desactivada_es_rechazada(self):
        inactivo = User(email='baja@test.cl', role=User.Role.CASINO, is_active=False)
        self.assertFalse(await self._conectar(inactivo))

    async def test_personal_del_casino_si_conecta(self):
        casino = User(email='casinows@test.cl', role=User.Role.CASINO)
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
            'degradado@test.cl', User.Role.CASINO,
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


class LoginRedirectTests(TestCase):
    """El login lleva a cada rol a su sección y no fuera del sitio."""

    @classmethod
    def setUpTestData(cls):
        cls.casino = crear_usuario('casino2@test.cl', User.Role.CASINO)
        cls.admin = crear_usuario('admin2@test.cl', User.Role.ADMIN)

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
        u = crear_usuario('super@test.cl', User.Role.CASINO)
        u.is_superuser = True
        self.assertTrue(u.is_admin)
        self.assertTrue(u.can_see_dashboard)

    def test_rol_por_defecto_es_el_mas_restringido(self):
        u = User.objects.create_user(email='nuevo@test.cl', password='x')
        self.assertEqual(u.role, User.Role.CASINO)
        self.assertFalse(u.can_see_dashboard)
        self.assertTrue(u.can_see_tickets)

    def test_bajar_el_rol_cierra_el_admin_de_django_tambien_con_update_fields(self):
        """
        Un save() parcial es el camino típico de un script de mantención. Si no
        arrastrara is_staff, el ex-administrador conservaría el panel /admin/ de Django,
        que da acceso a toda la configuración.
        """
        user = crear_usuario('exadmin@test.cl', User.Role.ADMIN)
        self.assertTrue(user.is_staff)

        user.role = User.Role.MANAGER
        user.save(update_fields=['role'])

        user.refresh_from_db()
        self.assertFalse(user.is_staff, 'is_staff quedó en la base pese al cambio de rol')
        self.client.force_login(user)
        self.assertEqual(self.client.get('/admin/').status_code, 302)

    def test_administrador_usa_el_admin_de_django_sin_ser_superusuario(self):
        """Tener is_staff sin permisos por modelo dejaría el panel vacío e inservible."""
        admin = crear_usuario('adminweb@test.cl', User.Role.ADMIN)
        self.assertFalse(admin.is_superuser)
        self.client.force_login(admin)
        resp = self.client.get('/admin/core/station/')
        self.assertEqual(resp.status_code, 200)

    def test_el_formulario_marca_staff_solo_para_administradores(self):
        from backend.apps.webapp.forms import UserForm

        form = UserForm(data={'email': 'g@test.cl', 'first_name': 'G', 'last_name': 'G',
                              'role': 'gerente', 'is_active': 'on', 'password': 'clave123'})
        self.assertTrue(form.is_valid(), form.errors)
        user = form.save()
        self.assertFalse(user.is_staff)
