"""
Nuevos roles del backoffice: administrador del sistema, gerente de administración y
personal del casino. Los roles «operator» y «viewer» desaparecen.

Los usuarios que los tenían pasan a «casino», el más restringido de los tres: quedarse
corto es recuperable (se les amplía el rol), quedarse largo es un acceso indebido.
"""
from django.db import migrations, models


def migrar_roles(apps, schema_editor):
    User = apps.get_model('core', 'User')
    User.objects.filter(role__in=['operator', 'viewer']).update(role='casino')
    # coherencia: solo el administrador entra al panel /admin de Django
    User.objects.exclude(role='admin').filter(is_superuser=False).update(is_staff=False)


def revertir_roles(apps, schema_editor):
    User = apps.get_model('core', 'User')
    User.objects.filter(role__in=['casino', 'gerente']).update(role='viewer')


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0007_shiftschedule_uid_unico'),
    ]

    operations = [
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.CharField(
                choices=[('admin', 'Administrador del sistema'),
                         ('gerente', 'Gerente de administración'),
                         ('casino', 'Personal del casino')],
                default='casino', max_length=10, verbose_name='rol',
            ),
        ),
        migrations.RunPython(migrar_roles, revertir_roles),
    ]
