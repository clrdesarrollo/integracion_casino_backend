from django.urls import path

from backend.apps.api.views import EnrollView, HealthcheckView, SyncView

app_name = 'api'

urlpatterns = [
    path('healthcheck/', HealthcheckView.as_view(), name='healthcheck'),
    path('enroll/', EnrollView.as_view(), name='enroll'),
    path('sync/', SyncView.as_view(), name='sync'),
]
