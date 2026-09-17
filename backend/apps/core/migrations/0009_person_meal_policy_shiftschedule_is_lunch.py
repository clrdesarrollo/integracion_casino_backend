"""
Colación asignada por persona (campo personalizado «Colacion» de HikCentral: 0 = sin
colación, 1 = solo almuerzo, 2 = todos los turnos; NULL = sin definir) y marca de
«turno de almuerzo» en los turnos programados, que es donde las personas con
«solo almuerzo» pueden retirar.

Al crear la columna se marca como almuerzo todo turno cuyo nombre lo diga; después
manda lo que se edite en el backoffice (viaja al terminal en la configuración compartida).
"""
from django.db import migrations, models


def marcar_almuerzos(apps, schema_editor):
    ShiftSchedule = apps.get_model('core', 'ShiftSchedule')
    ShiftSchedule.objects.filter(name__icontains='almuerzo').update(is_lunch=True)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0008_roles_gerente_casino'),
    ]

    operations = [
        migrations.AddField(
            model_name='person',
            name='meal_policy',
            field=models.SmallIntegerField(
                'colación asignada', blank=True, null=True,
                choices=[(0, 'Sin colación'), (1, 'Solo almuerzo'), (2, 'Todos los turnos')],
                help_text='Campo personalizado «Colacion» de HikCentral. Vacío = sin definir '
                          '(el terminal la atiende como «todos los turnos»).',
            ),
        ),
        migrations.AddField(
            model_name='shiftschedule',
            name='is_lunch',
            field=models.BooleanField(
                'es el turno de almuerzo', default=False,
                help_text='Las personas con colación «solo almuerzo» en HikCentral solo pueden '
                          'retirar en los turnos marcados como almuerzo.',
            ),
        ),
        migrations.RunPython(marcar_almuerzos, migrations.RunPython.noop),
    ]
