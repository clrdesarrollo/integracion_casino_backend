from django.urls import path

from backend.apps.reports import views

app_name = 'reports'

urlpatterns = [
    path('', views.report_view, name='report'),
    path('pdf/', views.report_pdf, name='report_pdf'),
    path('excel/', views.report_excel, name='report_excel'),
]
