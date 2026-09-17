from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

from backend.apps.authentication.forms import LoginForm


def _post_login_redirect(request, user):
    """
    A dónde va el usuario tras autenticarse: al destino que pedía, si lo había, y si
    no a la sección de entrada de su rol (el personal del casino no ve el panel).

    El `next` se valida contra el propio host: sin esa comprobación, un enlace como
    `/login/?next=https://otro-sitio/` llevaría al usuario fuera del backoffice justo
    después de escribir su contraseña.
    """
    next_url = request.POST.get('next') or request.GET.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        return redirect(next_url)

    home = user.home_url_name
    if home is None:
        raise PermissionDenied('Tu usuario no tiene acceso a ninguna sección.')
    return redirect(home)


def login_view(request):
    if request.user.is_authenticated:
        return _post_login_redirect(request, request.user)

    form = LoginForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = authenticate(
            request,
            username=form.cleaned_data['email'],
            password=form.cleaned_data['password'],
        )
        if user is not None:
            login(request, user)
            return _post_login_redirect(request, user)
        messages.error(request, 'Credenciales inválidas o usuario inactivo.')

    return render(request, 'authentication/login.html', {'form': form})


def logout_view(request):
    logout(request)
    messages.info(request, 'Sesión cerrada.')
    return redirect('login')
