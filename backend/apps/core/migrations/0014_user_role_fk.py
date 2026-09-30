"""Retira el rol de texto y deja el rol de la tabla como único rol del usuario."""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0013_users_to_roles'),
    ]

    operations = [
        migrations.RemoveField(model_name='user', name='role'),
        migrations.RenameField(model_name='user', old_name='role_new', new_name='role'),
        migrations.AlterField(
            model_name='user',
            name='role',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT,
                                    related_name='users', to='core.role', verbose_name='rol'),
        ),
    ]
