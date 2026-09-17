from django.apps import AppConfig


class RealtimeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend.apps.realtime'
    label = 'realtime'
    verbose_name = 'Tiempo real'
