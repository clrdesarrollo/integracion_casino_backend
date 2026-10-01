from django.db import migrations


def grant_shifts(apps, schema_editor):
    """
    Permiso nuevo «Detalle de turnos»: nace otorgado a los roles que ya ven Reportería, que
    es donde se consulta la misma información en resumen. Desde Roles se le puede quitar a
    quien no corresponda.
    """
    Role = apps.get_model('core', 'Role')
    for role in Role.objects.all():
        perms = list(role.permissions or [])
        if 'reports' in perms and 'shifts' not in perms:
            role.permissions = perms + ['shifts']
            role.save(update_fields=['permissions'])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0017_visit_meal_grant'),
    ]

    operations = [
        migrations.RunPython(grant_shifts, migrations.RunPython.noop),
    ]
