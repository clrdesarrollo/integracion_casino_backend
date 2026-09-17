"""
Control de acceso del backoffice.

Regla: estar autenticado NO da acceso a nada por sí solo. Cada vista declara qué
capacidad exige y el rol del usuario decide (ver `User.Role`):

- Administrador del sistema : todo, incluida la configuración (turnos, estaciones, usuarios).
- Gerente de administración : toda la información de colaciones emitidas + el panel.
- Personal del casino       : las colaciones emitidas (monitor, visitas, reportería).

Cuando se niega el acceso se redirige a la sección de entrada del propio usuario, nunca
a una página que tampoco puede ver: eso provocaría un bucle de redirecciones.
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth import logout
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

DENIED_MESSAGE = 'No tienes permisos para acceder a esa sección.'


def _guard(check):
    """Construye un decorador que exige `check(user)` para entrar a la vista."""

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            user = request.user
            if not user.is_authenticated:
                return redirect('login')
            if not user.is_active:
                # Defensa en profundidad: con el backend actual (ModelBackend) una cuenta
                # desactivada ya llega como anónima y no pasa de la comprobación anterior.
                # Esto la ataja igual, por si algún backend futuro autentica inactivos.
                logout(request)
                messages.error(request, 'Tu cuenta está desactivada.')
                return redirect('login')
            if not check(user):
                home = user.home_url_name
                # sin ninguna sección permitida no hay a dónde mandarlo: 403,
                # porque redirigir a una página igualmente vedada haría un bucle
                if home is None:
                    raise PermissionDenied(DENIED_MESSAGE)
                messages.error(request, DENIED_MESSAGE)
                return redirect(home)
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


#: Configuración del sistema: turnos, estaciones, API keys y usuarios.
admin_required = _guard(lambda u: u.is_admin)

#: Colaciones emitidas: monitor en vivo, control de visitas y reportería.
tickets_required = _guard(lambda u: u.can_see_tickets)

#: Panel con el resumen de la operación. El panel es además la raíz del sitio, así que
#: su vista despacha por su cuenta (ver `webapp.views.dashboard`) para no mostrar un
#: error de permisos a quien simplemente entró a "/" tras iniciar sesión.
dashboard_required = _guard(lambda u: u.can_see_dashboard)
