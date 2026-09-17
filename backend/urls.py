from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),
    path('api/', include('backend.apps.api.urls')),
    path('', include('backend.apps.authentication.urls')),
    path('', include('backend.apps.webapp.urls')),
    path('reportes/', include('backend.apps.reports.urls')),
]
