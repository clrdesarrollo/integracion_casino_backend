from django.apps import AppConfig


class WebappConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'backend.apps.webapp'
    label = 'webapp'
    verbose_name = 'Backoffice'
