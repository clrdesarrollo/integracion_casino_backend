"""
Control de acceso del backoffice.

Regla: estar autenticado NO da acceso a nada por sí solo. Cada vista declara qué
capacidad exige (ver `core.access`) y el rol del usuario decide (ver `core.Role`). Los roles
se editan desde el propio backoffice; el administrador del sistema tiene siempre todas.

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


def capability_required(capability):
    """Exige una capacidad del catálogo (`core.access.CAPABILITIES`)."""
    return _guard(lambda u: u.has_cap(capability))
