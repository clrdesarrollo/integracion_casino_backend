"""
Roles editables: crea la tabla de roles con los tres originales (administrador, gerente y
personal del casino) y prepara la columna que sustituirá al antiguo campo de texto.
Los usuarios se pasan al rol nuevo en 0013 y el campo viejo se retira en 0014 (separado
porque PostgreSQL no deja alterar una tabla con cambios de datos pendientes en la misma
transacción).
"""
from django.db import migrations, models
import django.db.models.deletion

from backend.apps.core.access import DEFAULT_ROLES


def sembrar_roles(apps, schema_editor):
    Role = apps.get_model('core', 'Role')
    for code, name, description, permissions in DEFAULT_ROLES:
        Role.objects.update_or_create(code=code, defaults={
            'name': name, 'description': description, 'permissions': list(permissions),
            'is_admin': code == 'admin', 'is_system': True,
        })


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0011_visit_registry'),
    ]

    operations = [
        migrations.CreateModel(
            name='Role',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=80, unique=True, verbose_name='nombre')),
                ('code', models.SlugField(editable=False, max_length=60, unique=True, verbose_name='código')),
                ('description', models.CharField(blank=True, max_length=255, verbose_name='descripción')),
                ('permissions', models.JSONField(blank=True, default=list, verbose_name='permisos')),
                ('is_admin', models.BooleanField(default=False, editable=False, verbose_name='acceso total')),
                ('is_system', models.BooleanField(default=False, editable=False, verbose_name='rol de sistema')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='creado')),
            ],
            options={
                'verbose_name': 'rol',
                'verbose_name_plural': 'roles',
                'db_table': 'tb_role',
                'ordering': ['-is_admin', 'name'],
            },
        ),
        migrations.RunPython(sembrar_roles, migrations.RunPython.noop),
        migrations.AddField(
            model_name='user',
            name='role_new',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.PROTECT,
                                    related_name='+', to='core.role'),
        ),
    ]
