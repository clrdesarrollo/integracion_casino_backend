from django.urls import path

from backend.apps.reports import views

app_name = 'reports'

urlpatterns = [
    path('', views.report_view, name='report'),
    path('pdf/', views.report_pdf, name='report_pdf'),
    path('excel/', views.report_excel, name='report_excel'),

    # Detalle de colaciones: cada apertura con su inicio/término y quién marcó, a qué hora
    path('turnos/', views.shift_list, name='shift_list'),
    path('turnos/<int:pk>/', views.shift_detail, name='shift_detail'),
    path('turnos/<int:pk>/excel/', views.shift_excel, name='shift_excel'),
]
