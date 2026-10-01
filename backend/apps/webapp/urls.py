from django.urls import path

from backend.apps.webapp import views

app_name = 'webapp'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
    path('monitor/', views.monitor, name='monitor'),
    path('monitor/turnos/', views.monitor_shifts, name='monitor_shifts'),

    # Visitas: registro de entrega de tarjetas, colaciones (pagos adicionales) con foto,
    # e inventario de tarjetas físicas (solo administrador)
    path('visitas/', views.visit_list, name='visit_list'),
    path('visitas/<int:pk>/devolver/', views.visit_return, name='visit_return'),
    path('visitas/<int:pk>/eliminar/', views.visit_delete, name='visit_delete'),
    path('visitas/funcionarios/', views.visit_person_suggest, name='visit_person_suggest'),
    path('visitas/colaciones/', views.visitor_events, name='visitor_events'),
    path('visitas/<int:pk>/foto/', views.visitor_event_photo, name='visitor_event_photo'),
    path('visitas/tarjetas/', views.visitor_cards_index, name='visitor_cards_index'),

    # Usuarios
    path('usuarios/', views.user_list, name='user_list'),
    path('usuarios/nuevo/', views.user_create, name='user_create'),
    path('usuarios/<int:pk>/editar/', views.user_edit, name='user_edit'),
    path('usuarios/<int:pk>/eliminar/', views.user_delete, name='user_delete'),

    # Roles (qué puede hacer cada tipo de usuario)
    path('roles/', views.role_list, name='role_list'),
    path('roles/nuevo/', views.role_create, name='role_create'),
    path('roles/<int:pk>/editar/', views.role_edit, name='role_edit'),
    path('roles/<int:pk>/eliminar/', views.role_delete, name='role_delete'),

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
    path('estaciones/<int:pk>/modo-pruebas/', views.station_test_mode, name='station_test_mode'),
    path('estaciones/<int:pk>/turnos-automaticos/', views.station_auto_shifts, name='station_auto_shifts'),
    path('estaciones/<int:pk>/turnos/<int:sid>/', views.schedule_edit, name='schedule_edit'),
    path('estaciones/<int:pk>/turnos/<int:sid>/eliminar/', views.schedule_delete, name='schedule_delete'),

    # Inventario de tarjetas RFID de visitas (por estación; se llega desde Visitas → Tarjetas)
    path('estaciones/<int:pk>/tarjetas/', views.visitor_card_list, name='visitor_card_list'),
    path('estaciones/<int:pk>/tarjetas/<int:cid>/alternar/', views.visitor_card_toggle, name='visitor_card_toggle'),
    path('estaciones/<int:pk>/tarjetas/<int:cid>/eliminar/', views.visitor_card_delete, name='visitor_card_delete'),

    # Personas de la estación con su colación asignada (solo lectura: viene de HikCentral)
    path('estaciones/<int:pk>/personas/', views.person_list, name='person_list'),
    path('estaciones/<int:pk>/personas/actualizar/', views.person_refresh, name='person_refresh'),
    path('personas/<int:pk>/foto/', views.person_photo, name='person_photo'),
]
