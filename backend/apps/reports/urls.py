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

    # Ingreso manual de colaciones: sobre un turno cerrado, eligiendo a la persona de la ficha
    path('ingresos-manuales/', views.manual_event_list, name='manual_event_list'),
    path('turnos/<int:pk>/ingreso-manual/', views.manual_event_create, name='manual_event_create'),
    path('turnos/<int:pk>/ingreso-manual/personas/', views.manual_event_persons,
         name='manual_event_persons'),
    path('ingresos-manuales/<int:pk>/anular/', views.manual_event_annul, name='manual_event_annul'),

    # Colaciones programadas (entrega en mano, sin marcar en el kiosco) y feriados
    path('colaciones-programadas/', views.standing_meal_list, name='standing_meal_list'),
    path('colaciones-programadas/nueva/', views.standing_meal_create, name='standing_meal_create'),
    path('colaciones-programadas/personas/', views.standing_meal_persons, name='standing_meal_persons'),
    path('colaciones-programadas/<int:pk>/', views.standing_meal_edit, name='standing_meal_edit'),
    path('colaciones-programadas/<int:pk>/alternar/', views.standing_meal_toggle, name='standing_meal_toggle'),
    path('colaciones-programadas/<int:pk>/eliminar/', views.standing_meal_delete, name='standing_meal_delete'),
    path('colaciones-programadas/<int:pk>/excepciones/', views.standing_exception_create,
         name='standing_exception_create'),
    path('colaciones-programadas/<int:pk>/excepciones/<int:eid>/eliminar/', views.standing_exception_delete,
         name='standing_exception_delete'),
    path('feriados/', views.holiday_list, name='holiday_list'),
    path('feriados/<int:pk>/eliminar/', views.holiday_delete, name='holiday_delete'),
    path('feriados/cargar-chile/', views.holiday_load_cl, name='holiday_load_cl'),

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
