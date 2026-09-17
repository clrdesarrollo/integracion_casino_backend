"""
Los turnos programados que ya existían quedaron todos con el MISMO uid.

`AddField` con un `default` invocable lo evalúa una sola vez y lo aplica a todas las
filas, así que la migración 0005 les puso a todos el mismo valor. El uid es lo que
agrupa las colaciones de un servicio (Shift.schedule_uid): con uno compartido, quien
desayunó quedaría marcado como repetido al almorzar. Aquí se le da uno propio a cada uno.
"""
import uuid

from django.db import migrations


def asignar_uids_unicos(apps, schema_editor):
    ShiftSchedule = apps.get_model('core', 'ShiftSchedule')
    vistos = set()
    for sched in ShiftSchedule.objects.order_by('id'):
        if sched.uid and sched.uid not in vistos:
            vistos.add(sched.uid)
            continue
        nuevo = uuid.uuid4().hex
        # los turnos ya sincronizados por el terminal traen su propio uid y no se tocan;
        # aquí solo se reparan los repetidos (o vacíos) que dejó la migración anterior
        ShiftSchedule.objects.filter(pk=sched.pk).update(uid=nuevo)
        vistos.add(nuevo)


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0006_remove_accessevent_uq_event_station_remote_and_more'),
    ]

    operations = [
        migrations.RunPython(asignar_uids_unicos, migrations.RunPython.noop),
    ]
