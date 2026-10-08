from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from rest_framework_api_key.admin import APIKeyModelAdmin

from backend.apps.core.models import (
    AuditLog, Role, User, Station, StationAPIKey, Person, Shift, AccessEvent,
    ShiftSchedule, ShiftScheduleCompany, Visit, VisitorCard,
)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Solo lectura: la bitácora no se edita ni se borra, ni siquiera desde /admin."""

    list_display = ('at', 'category', 'action', 'level', 'summary', 'station_name', 'user_name')
    list_filter = ('category', 'level', 'station')
    search_fields = ('summary', 'user_name', 'action')
    date_hierarchy = 'at'
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class UserCreationFormEmail(UserCreationForm):
    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name', 'role')


class UserChangeFormEmail(UserChangeForm):
    class Meta:
        model = User
        fields = '__all__'


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'is_admin', 'is_system')
    readonly_fields = ('code', 'is_admin', 'is_system')


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = UserCreationFormEmail
    form = UserChangeFormEmail
    model = User

    list_display = ('email', 'first_name', 'last_name', 'role', 'is_active', 'is_staff')
    list_filter = ('role', 'is_active', 'is_staff')
    list_select_related = ('role',)
    # el rol define el acceso al backoffice: conviene verlo al listar y poder filtrarlo
    search_fields = ('email', 'first_name', 'last_name')
    ordering = ('email',)
    # is_staff lo deriva User.save() del rol: editable engañaría (el cambio no se aplica)
    readonly_fields = ('is_staff',)

    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        ('Datos personales', {'fields': ('first_name', 'last_name')}),
        ('Permisos', {'fields': ('role', 'is_active', 'is_staff', 'is_superuser',
                                 'groups', 'user_permissions')}),
        ('Fechas', {'fields': ('last_login', 'date_joined')}),
    )
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'first_name', 'last_name', 'role',
                       'password1', 'password2'),
        }),
    )


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ('name', 'location', 'is_active', 'last_sync_at', 'created_at')
    list_filter = ('is_active',)
    search_fields = ('name', 'location')


@admin.register(StationAPIKey)
class StationAPIKeyAdmin(APIKeyModelAdmin):
    list_display = ('prefix', 'station', 'name', 'created', 'expiry_date', 'revoked')
    list_filter = ('station', 'revoked')
    search_fields = ('prefix', 'name', 'station__name')


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ('name', 'employee_no', 'company', 'user_type', 'authorized', 'meal_policy', 'station')
    list_filter = ('station', 'user_type', 'authorized', 'meal_policy', 'company')
    search_fields = ('name', 'employee_no', 'company')


@admin.register(Shift)
class ShiftAdmin(admin.ModelAdmin):
    list_display = ('name', 'station', 'started_at', 'ended_at', 'end_reason',
                    'service_date', 'auto', 'is_reopening')
    list_filter = ('station', 'auto', 'end_reason')
    search_fields = ('name', 'uid', 'schedule_uid')
    date_hierarchy = 'started_at'


@admin.register(AccessEvent)
class AccessEventAdmin(admin.ModelAdmin):
    list_display = ('event_time', 'person_name', 'employee_no', 'company', 'status',
                    'is_visitor', 'has_photo', 'station')
    list_filter = ('station', 'status', 'is_visitor', 'company')
    search_fields = ('person_name', 'employee_no', 'company', 'card_no', 'uid')
    date_hierarchy = 'event_time'


class ShiftScheduleCompanyInline(admin.TabularInline):
    model = ShiftScheduleCompany
    extra = 0


@admin.register(ShiftSchedule)
class ShiftScheduleAdmin(admin.ModelAdmin):
    list_display = ('name', 'station', 'time_range', 'enabled', 'all_companies', 'allow_visitors')
    list_filter = ('station', 'enabled', 'all_companies', 'allow_visitors')
    search_fields = ('name',)
    inlines = [ShiftScheduleCompanyInline]


@admin.register(VisitorCard)
class VisitorCardAdmin(admin.ModelAdmin):
    list_display = ('card_no', 'label', 'station', 'enabled', 'created_at')
    list_filter = ('station', 'enabled')
    search_fields = ('card_no', 'label')


@admin.register(Visit)
class VisitAdmin(admin.ModelAdmin):
    list_display = ('delivered_at', 'visitor_name', 'card_no', 'card_label', 'host_name',
                    'host_company', 'delivered_by_name', 'returned_at', 'station')
    list_filter = ('station', 'host_company')
    search_fields = ('visitor_name', 'visitor_document', 'host_name', 'host_company',
                     'card_no', 'card_label', 'delivered_by_name')
    date_hierarchy = 'delivered_at'
    raw_id_fields = ('host_person', 'delivered_by', 'returned_by')
