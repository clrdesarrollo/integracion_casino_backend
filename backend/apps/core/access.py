"""
Catálogo de capacidades del backoffice.

Un rol (`core.Role`) es un conjunto de estas capacidades; cada vista exige una y el menú se
dibuja con las mismas. Agregar una sección nueva es declarar aquí su capacidad, protegerla
con `capability_required('<codigo>')` y listarla en el formulario de roles (sale sola).
"""
from collections import namedtuple

Capability = namedtuple('Capability', 'code label help group home')

CAPABILITIES = (
    Capability('dashboard', 'Panel', 'Resumen de la operación del mes.',
               'Operación', 'webapp:dashboard'),
    Capability('monitor', 'Monitor en vivo', 'Colaciones a medida que se marcan en el kiosco.',
               'Operación', 'webapp:monitor'),
    Capability('reports', 'Informes', 'Informe de colaciones con descarga en PDF y Excel.',
               'Operación', 'reports:report'),
    Capability('shifts', 'Detalle de colaciones',
               'Dentro de Informes: cada turno realizado, cómo se abrió y cerró, y quién marcó y a qué hora.',
               'Operación', 'reports:shift_list'),
    Capability('visits', 'Registro de visitas',
               'Entregar y recibir tarjetas de visita (trámite de mesón).',
               'Visitas', 'webapp:visit_list'),
    Capability('visit_events', 'Colaciones de visitas',
               'Marcaciones con tarjeta de visita y su foto.',
               'Visitas', 'webapp:visitor_events'),
    Capability('visit_cards', 'Administrar visitas',
               'Eliminar registros de visita y mantener el inventario de tarjetas.',
               'Visitas', 'webapp:visitor_cards_index'),
    Capability('config', 'Turnos, empresas y estaciones',
               'Configuración compartida con el kiosco, estaciones y sus API keys.',
               'Configuración', 'webapp:config_index'),
    Capability('users', 'Usuarios y roles',
               'Crear usuarios y definir qué puede hacer cada rol.',
               'Configuración', 'webapp:user_list'),
)

CAPABILITY_CODES = tuple(c.code for c in CAPABILITIES)
CAPABILITY_LABELS = {c.code: c.label for c in CAPABILITIES}

# Permisos con los que nacen los tres roles originales (migración 0012).
DEFAULT_ROLES = (
    ('admin', 'Administrador del sistema', 'Acceso total, incluida la configuración.',
     CAPABILITY_CODES),
    ('gerente', 'Gerente de administración', 'Panel, colaciones y reportería.',
     ('dashboard', 'monitor', 'reports', 'shifts', 'visits', 'visit_events')),
    ('casino', 'Personal del casino', 'Monitor, visitas y reportería.',
     ('monitor', 'reports', 'shifts', 'visits', 'visit_events')),
)
