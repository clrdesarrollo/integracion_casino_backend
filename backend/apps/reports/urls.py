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

    # Envíos por correo: el informe en PDF cada cierto tiempo, su historial y el servidor SMTP
    path('envios/', views.mail_schedule_list, name='mail_schedule_list'),
    path('envios/nuevo/', views.mail_schedule_create, name='mail_schedule_create'),
    path('envios/<int:pk>/editar/', views.mail_schedule_edit, name='mail_schedule_edit'),
    path('envios/<int:pk>/eliminar/', views.mail_schedule_delete, name='mail_schedule_delete'),
    path('envios/<int:pk>/enviar/', views.mail_schedule_send, name='mail_schedule_send'),
    path('envios/historial/', views.mail_delivery_list, name='mail_delivery_list'),
    path('envios/historial/<int:pk>/reintentar/', views.mail_delivery_retry,
         name='mail_delivery_retry'),
    path('correo/', views.mail_settings, name='mail_settings'),
    path('correo/prueba/', views.mail_settings_test, name='mail_settings_test'),
]
