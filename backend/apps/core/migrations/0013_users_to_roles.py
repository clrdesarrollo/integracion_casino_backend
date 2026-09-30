"""Pasa cada usuario de su antiguo rol de texto al rol equivalente de la tabla de roles."""
from django.db import migrations


def asignar_roles(apps, schema_editor):
    User = apps.get_model('core', 'User')
    Role = apps.get_model('core', 'Role')
    roles = {r.code: r for r in Role.objects.all()}
    for code, role in roles.items():
        User.objects.filter(role=code).update(role_new=role)
    # valor desconocido (no debería haberlo): el rol más restringido
    User.objects.filter(role_new__isnull=True).update(role_new=roles['casino'])


def devolver_roles(apps, schema_editor):
    User = apps.get_model('core', 'User')
    for user in User.objects.select_related('role_new'):
        # un rol creado a mano no existe en el esquema viejo: el más restringido
        code = user.role_new.code if user.role_new.code in ('admin', 'gerente', 'casino') else 'casino'
        User.objects.filter(pk=user.pk).update(role=code)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0012_role_model'),
    ]

    operations = [
        migrations.RunPython(asignar_roles, devolver_roles),
    ]
