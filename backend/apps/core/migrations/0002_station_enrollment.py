from django.db import migrations, models


def assign_device_ids(apps, schema_editor):
    """Asigna IDs de dispositivo correlativos (desde 1000) a las estaciones existentes."""
    Station = apps.get_model('core', 'Station')
    next_id = 1000
    for station in Station.objects.order_by('id'):
        station.device_id = next_id
        station.save(update_fields=['device_id'])
        next_id += 1


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='station',
            name='device_id',
            field=models.PositiveIntegerField(
                null=True, verbose_name='ID de dispositivo',
                help_text='Código numérico que se ingresa en el terminal para enrolarlo (ej. 1000).',
            ),
        ),
        migrations.AddField(
            model_name='station',
            name='enroll_password',
            field=models.CharField(
                blank=True, default='', max_length=128,
                verbose_name='contraseña de enrolado',
            ),
        ),
        migrations.RunPython(assign_device_ids, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='station',
            name='device_id',
            field=models.PositiveIntegerField(
                unique=True, verbose_name='ID de dispositivo',
                help_text='Código numérico que se ingresa en el terminal para enrolarlo (ej. 1000).',
            ),
        ),
    ]
