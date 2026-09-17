from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import UserCreationForm, UserChangeForm
from rest_framework_api_key.admin import APIKeyModelAdmin

from backend.apps.core.models import (
    User, Station, StationAPIKey, Person, Shift, AccessEvent,
    ShiftSchedule, ShiftScheduleCompany, VisitorCard,
)


class UserCreationFormEmail(UserCreationForm):
    class Meta:
        model = User
        fields = ('email', 'first_name', 'last_name', 'role')


class UserChangeFormEmail(UserChangeForm):
    class Meta:
        model = User
        fields = '__all__'


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    add_form = UserCreationFormEmail
    form = UserChangeFormEmail
    model = User

    list_display = ('email', 'first_name', 'last_name', 'role', 'is_active', 'is_staff')
    list_filter = ('role', 'is_active', 'is_staff')
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
