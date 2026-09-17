from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = 'Crea el superusuario inicial desde DJANGO_SUPERUSER_EMAIL/PASSWORD si no existe.'

    def handle(self, *args, **options):
        User = get_user_model()
        email = settings.DJANGO_SUPERUSER_EMAIL
        password = settings.DJANGO_SUPERUSER_PASSWORD

        if not email or not password:
            self.stdout.write(self.style.WARNING(
                'DJANGO_SUPERUSER_EMAIL/PASSWORD no definidos; se omite la creación.'))
            return

        if User.objects.filter(email=email).exists():
            self.stdout.write(f'El superusuario {email} ya existe.')
            return

        User.objects.create_superuser(
            email=email,
            password=password,
            first_name='Administrador',
            last_name='CLRobotics',
        )
        self.stdout.write(self.style.SUCCESS(f'Superusuario {email} creado.'))
