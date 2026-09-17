from django.urls import path

from backend.apps.webapp import views

app_name = 'webapp'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('monitor/', views.monitor, name='monitor'),
    path('monitor/turnos/', views.monitor_shifts, name='monitor_shifts'),

    # Colaciones de visitas (pagos adicionales): control con foto
    path('visitas/', views.visitor_events, name='visitor_events'),
    path('visitas/<int:pk>/foto/', views.visitor_event_photo, name='visitor_event_photo'),

    # Usuarios
    path('usuarios/', views.user_list, name='user_list'),
    path('usuarios/nuevo/', views.user_create, name='user_create'),
    path('usuarios/<int:pk>/editar/', views.user_edit, name='user_edit'),
    path('usuarios/<int:pk>/eliminar/', views.user_delete, name='user_delete'),

    # Estaciones
    path('estaciones/', views.station_list, name='station_list'),
    path('estaciones/nueva/', views.station_create, name='station_create'),
    path('estaciones/<int:pk>/', views.station_detail, name='station_detail'),
    path('estaciones/<int:pk>/api-key/nueva/', views.station_api_key_create,
         name='station_api_key_create'),
    path('estaciones/<int:pk>/api-key/<str:key_id>/revocar/', views.station_api_key_revoke,
         name='station_api_key_revoke'),

    # Turnos programados + empresas autorizadas (configuración compartida con el terminal)
    path('turnos/', views.config_index, name='config_index'),
    path('estaciones/<int:pk>/turnos/', views.schedule_list, name='schedule_list'),
    path('estaciones/<int:pk>/turnos/nuevo/', views.schedule_create, name='schedule_create'),
    path('estaciones/<int:pk>/turnos/<int:sid>/', views.schedule_edit, name='schedule_edit'),
    path('estaciones/<int:pk>/turnos/<int:sid>/eliminar/', views.schedule_delete, name='schedule_delete'),

    # Tarjetas RFID de visitas
    path('estaciones/<int:pk>/tarjetas/', views.visitor_card_list, name='visitor_card_list'),
    path('estaciones/<int:pk>/tarjetas/<int:cid>/alternar/', views.visitor_card_toggle, name='visitor_card_toggle'),
    path('estaciones/<int:pk>/tarjetas/<int:cid>/eliminar/', views.visitor_card_delete, name='visitor_card_delete'),

    # Personas de la estación con su colación asignada (solo lectura: viene de HikCentral)
    path('estaciones/<int:pk>/personas/', views.person_list, name='person_list'),
]
