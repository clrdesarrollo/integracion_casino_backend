from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from backend.apps.core.models import DAY_NAMES_LONG, Station
from backend.apps.reports.models import MailSettings, ScheduledReport, split_recipients
from backend.apps.reports.service import shift_names

_INPUT = {'class': 'form-control'}
_SELECT = {'class': 'form-select'}
_CHECK = {'class': 'form-check-input'}

#: Tope de destinatarios por envío (los servidores SMTP suelen limitar por mensaje).
MAX_RECIPIENTS = 50


class MailSettingsForm(forms.ModelForm):
    """Servidor SMTP. La contraseña no se muestra nunca: en blanco = no cambiarla."""

    password = forms.CharField(
        label='Contraseña', required=False, strip=False,
        widget=forms.PasswordInput(attrs={**_INPUT, 'autocomplete': 'new-password'}),
        help_text='Déjala en blanco para no cambiarla.',
    )

    class Meta:
        model = MailSettings
        fields = ['host', 'port', 'security', 'username', 'from_email', 'from_name']
        widgets = {
            'host': forms.TextInput(attrs={**_INPUT, 'placeholder': 'Ej: smtp.office365.com'}),
            'port': forms.NumberInput(attrs={**_INPUT, 'min': 1, 'max': 65535}),
            'security': forms.Select(attrs=_SELECT),
            'username': forms.TextInput(attrs={**_INPUT, 'autocomplete': 'off'}),
            'from_email': forms.EmailInput(attrs={**_INPUT, 'placeholder': 'informes@empresa.cl'}),
            'from_name': forms.TextInput(attrs=_INPUT),
        }
        help_texts = {
            'username': 'En blanco si el servidor no pide autenticación.',
            'from_email': 'Dirección desde la que salen los informes. Muchos servidores exigen '
                          'que sea la misma cuenta del usuario.',
        }

    def clean(self):
        cleaned = super().clean()
        host = (cleaned.get('host') or '').strip()
        cleaned['host'] = host
        if host and not cleaned.get('from_email') and 'from_email' not in self.errors:
            self.add_error('from_email', 'Indica el correo del remitente.')
        username = (cleaned.get('username') or '').strip()
        cleaned['username'] = username
        if (username and not cleaned.get('password')
                and (not self.instance.has_password or self.instance.password_unreadable)):
            self.add_error('password', 'Ingresa la contraseña de la cuenta.')
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        if not obj.username:
            obj.set_password('')     # sin usuario no hay autenticación: no guardar una clave suelta
        elif self.cleaned_data.get('password'):
            obj.set_password(self.cleaned_data['password'])
        if commit:
            obj.save()
        return obj


class TestMailForm(forms.Form):
    to = forms.EmailField(label='Enviar un correo de prueba a',
                          widget=forms.EmailInput(attrs=_INPUT))


class ManualEventForm(forms.Form):
    """
    Ingreso manual de UNA colación sobre un turno cerrado. La persona se elige desde la
    búsqueda (va su id de la ficha); las validaciones de fondo las hace `reports.manual`.
    """

    person = forms.IntegerField(widget=forms.HiddenInput())
    event_time = forms.TimeField(
        label='Hora de la colación', input_formats=['%H:%M:%S', '%H:%M'],
        widget=forms.TimeInput(attrs={**_INPUT, 'type': 'time', 'step': 1}, format='%H:%M:%S'),
        help_text='Si no la sabes con exactitud, deja la hora de término del turno.')
    reason = forms.CharField(
        label='Motivo', min_length=10, max_length=300,
        widget=forms.Textarea(attrs={**_INPUT, 'rows': 2,
                                     'placeholder': 'Ej: corte de energía, el kiosco quedó apagado; '
                                                    'anotado en la hoja de la cocina'}),
        help_text='Queda en el informe y en la bitácora junto a tu nombre.')
    # Solo se ofrece cuando la persona tiene avisos (el terminal la habría rechazado).
    override = forms.BooleanField(
        label='Registrar de todos modos', required=False,
        widget=forms.CheckboxInput(attrs=_CHECK))
    confirm = forms.BooleanField(
        label='Confirmo que esta persona retiró su colación en este turno y entiendo que el '
              'registro quedará marcado como ingreso manual a mi nombre.',
        widget=forms.CheckboxInput(attrs=_CHECK),
        error_messages={'required': 'Debes confirmar el registro.'})

    def clean_reason(self):
        return ' '.join(self.cleaned_data['reason'].split())


class ManualVisitorForm(forms.Form):
    """
    Ingreso manual de la colación de UNA visita que no pasó por el terminal (sin tarjeta):
    la visita se describe como en el registro de visitas. Va con prefijo porque comparte
    página con `ManualEventForm`. Las validaciones de fondo las hace `reports.manual`.
    """

    visitor_name = forms.CharField(
        label='Nombre de la visita', max_length=200,
        widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Ej: Juan Pérez', 'autocomplete': 'off'}))
    visitor_document = forms.CharField(
        label='RUT / documento', max_length=40, required=False,
        widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Opcional'}))
    visitor_company = forms.CharField(
        label='Empresa de procedencia', max_length=200, required=False,
        widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Opcional: de dónde viene'}))
    host_company = forms.CharField(
        label='Empresa que visitó', max_length=200, required=False,
        widget=forms.TextInput(attrs={**_INPUT, 'list': 'empresas-conocidas', 'autocomplete': 'off',
                                      'placeholder': 'Ej: PTJ'}))
    host_name = forms.CharField(
        label='Funcionario a quien vino a ver', max_length=200, required=False,
        widget=forms.TextInput(attrs={**_INPUT, 'list': 'funcionarios-sugeridos', 'autocomplete': 'off',
                                      'placeholder': 'Escribe el nombre para buscarlo'}))
    event_time = forms.TimeField(
        label='Hora de la colación', input_formats=['%H:%M:%S', '%H:%M'],
        widget=forms.TimeInput(attrs={**_INPUT, 'type': 'time', 'step': 1}, format='%H:%M:%S'),
        help_text='Si no la sabes con exactitud, deja la hora de término del turno.')
    reason = forms.CharField(
        label='Motivo', min_length=10, max_length=300,
        widget=forms.Textarea(attrs={**_INPUT, 'rows': 2,
                                     'placeholder': 'Ej: vino a reunión con gerencia y el almuerzo se '
                                                    'llevó a la sala de reuniones'}),
        help_text='Queda en el informe y en la bitácora junto a tu nombre.')
    # Solo se ofrece cuando el turno tiene avisos (no admite visitas).
    override = forms.BooleanField(
        label='Registrar de todos modos', required=False,
        widget=forms.CheckboxInput(attrs=_CHECK))
    confirm = forms.BooleanField(
        label='Confirmo que esta visita recibió su colación en este turno y entiendo que el '
              'registro quedará marcado como ingreso manual a mi nombre.',
        widget=forms.CheckboxInput(attrs=_CHECK),
        error_messages={'required': 'Debes confirmar el registro.'})

    def clean_visitor_name(self):
        name = ' '.join(self.cleaned_data['visitor_name'].split())
        if not name:
            raise ValidationError('Ingresa el nombre de la visita.')
        return name

    def clean_reason(self):
        return ' '.join(self.cleaned_data['reason'].split())

    def clean(self):
        data = super().clean()
        if not (data.get('host_company') or '').strip() and not (data.get('host_name') or '').strip():
            raise ValidationError('Indica a quién vino a ver: la empresa, el funcionario o ambos.')
        return data


class AnnulEventForm(forms.Form):
    reason = forms.CharField(label='Motivo de la anulación', min_length=5, max_length=300,
                             widget=forms.TextInput(attrs=_INPUT))

    def clean_reason(self):
        return ' '.join(self.cleaned_data['reason'].split())


class StandingMealForm(forms.Form):
    """
    Alta/edición de una colación programada. La persona se elige desde la búsqueda en la
    ficha de la estación (va su nº en el campo oculto); el turno, de los programados de la
    estación con marcación. Los días van como casillas y se guardan como máscara.
    """

    employee_no = forms.CharField(widget=forms.HiddenInput())
    schedule_uid = forms.ChoiceField(label='Turno', widget=forms.Select(attrs=_SELECT))
    weekdays = forms.MultipleChoiceField(
        label='Días', choices=[(str(i), n.capitalize()) for i, n in enumerate(DAY_NAMES_LONG)],
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
        error_messages={'required': 'Marca al menos un día.'})
    valid_from = forms.DateField(
        label='Vigente desde', widget=forms.DateInput(attrs={**_INPUT, 'type': 'date'}, format='%Y-%m-%d'))
    valid_to = forms.DateField(
        label='Vigente hasta', required=False,
        widget=forms.DateInput(attrs={**_INPUT, 'type': 'date'}, format='%Y-%m-%d'),
        help_text='En blanco: sin fecha de término.')
    grace_minutes = forms.IntegerField(
        label='Tolerancia sin cierre (minutos)', min_value=0, max_value=720,
        widget=forms.NumberInput(attrs={**_INPUT, 'min': 0, 'max': 720}),
        help_text='Si el terminal no informa el cierre del turno, se registra igual pasados estos '
                  'minutos desde el término programado más la prórroga de cierre de la estación.')
    note = forms.CharField(
        label='Motivo', min_length=10, max_length=300,
        widget=forms.Textarea(attrs={**_INPUT, 'rows': 2,
                                     'placeholder': 'Ej: cubre turno de 8 a 20 lejos del casino; '
                                                    'la once se la lleva el personal del casino'}),
        help_text='Queda como motivo en cada colación registrada, en el informe y en la bitácora.')
    active = forms.BooleanField(
        label='Activa', required=False,
        widget=forms.CheckboxInput(attrs={**_CHECK, 'role': 'switch'}))

    def __init__(self, *args, station, instance=None, **kwargs):
        from django.utils import timezone
        initial = dict(kwargs.pop('initial', None) or {})
        if instance is not None:
            initial.update(
                employee_no=instance.employee_no, schedule_uid=instance.schedule_uid,
                weekdays=[str(i) for i in range(7) if instance.days_mask & (1 << i)],
                valid_from=instance.valid_from, valid_to=instance.valid_to,
                grace_minutes=instance.grace_minutes, note=instance.note, active=instance.active)
        else:
            initial.setdefault('weekdays', ['0', '1', '2', '3'])
            initial.setdefault('valid_from', timezone.localdate())
            initial.setdefault('grace_minutes', 60)
            initial.setdefault('active', True)
        super().__init__(*args, initial=initial, **kwargs)
        self.station = station
        self.instance = instance
        self.person = None
        self.schedules = {
            s.uid: s for s in station.schedules.filter(enabled=True, manual_entry=False).order_by('start_min')
        }
        choices = [('', '— Elige el turno —')] + [
            (uid, f'{s.name} ({s.time_range}) · {s.days_text}') for uid, s in self.schedules.items()]
        if instance is not None and instance.schedule_uid not in self.schedules:
            choices.append((instance.schedule_uid,
                            f'{instance.schedule_name} (ya no existe en la configuración)'))
        self.fields['schedule_uid'].choices = choices

    def clean_employee_no(self):
        number = (self.cleaned_data['employee_no'] or '').strip()
        self.person = self.station.persons.exclude(user_type='visitor').filter(employee_no=number).first()
        if self.person is None:
            raise ValidationError('Elige a la persona desde la búsqueda (debe estar en la ficha de '
                                  'la estación).')
        return number

    def clean_note(self):
        return ' '.join(self.cleaned_data['note'].split())

    def clean(self):
        data = super().clean()
        start, end = data.get('valid_from'), data.get('valid_to')
        if start and end and end < start:
            self.add_error('valid_to', 'La fecha de término no puede ser anterior al inicio.')
        return data

    def save(self, user):
        from backend.apps.core.models import StandingMeal
        d = self.cleaned_data
        schedule = self.schedules.get(d['schedule_uid'])
        meal = self.instance
        if meal is None:
            meal = StandingMeal(station=self.station,
                                created_by=user if user.pk else None,
                                created_by_name=user.full_name)
        meal.employee_no = self.person.employee_no
        meal.person_name = self.person.name
        meal.company = self.person.company or ''
        meal.schedule_uid = d['schedule_uid']
        if schedule is not None:
            meal.schedule_name = schedule.name
        meal.days_mask = sum(1 << int(i) for i in d['weekdays'])
        meal.valid_from = d['valid_from']
        meal.valid_to = d.get('valid_to')
        meal.grace_minutes = d['grace_minutes']
        meal.note = d['note']
        meal.active = bool(d.get('active'))
        meal.save()
        return meal


class StandingExceptionForm(forms.Form):
    """Días en que la colación programada no se registra (vacaciones, licencia, un evento)."""

    date_from = forms.DateField(
        label='Desde', widget=forms.DateInput(attrs={**_INPUT, 'type': 'date'}, format='%Y-%m-%d'))
    date_to = forms.DateField(
        label='Hasta', widget=forms.DateInput(attrs={**_INPUT, 'type': 'date'}, format='%Y-%m-%d'))
    reason = forms.CharField(
        label='Motivo', max_length=200,
        widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Ej: vacaciones'}))

    def clean(self):
        data = super().clean()
        start, end = data.get('date_from'), data.get('date_to')
        if start and end and end < start:
            self.add_error('date_to', 'La fecha de término no puede ser anterior al inicio.')
        return data

    def clean_reason(self):
        return ' '.join(self.cleaned_data['reason'].split())


class HolidayForm(forms.Form):
    date = forms.DateField(
        label='Fecha', widget=forms.DateInput(attrs={**_INPUT, 'type': 'date'}, format='%Y-%m-%d'))
    name = forms.CharField(
        label='Nombre', max_length=120,
        widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Ej: Día de las Glorias Navales'}))

    def clean_name(self):
        return ' '.join(self.cleaned_data['name'].split())


class ScheduledReportForm(forms.ModelForm):
    """Alta/edición de un envío programado."""

    weekdays = forms.MultipleChoiceField(
        label='Días de envío', required=False,
        choices=[(str(i), n.capitalize()) for i, n in enumerate(DAY_NAMES_LONG)],
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
    )
    shift_name = forms.ChoiceField(label='Turno', required=False,
                                   widget=forms.Select(attrs=_SELECT))
    station = forms.ModelChoiceField(label='Estación', required=False,
                                     queryset=Station.objects.all(),
                                     empty_label='Todas las estaciones',
                                     widget=forms.Select(attrs=_SELECT))

    class Meta:
        model = ScheduledReport
        fields = ['name', 'is_active', 'frequency', 'month_day', 'send_time', 'period',
                  'period_days', 'station', 'shift_name', 'attach_excel', 'recipients',
                  'subject', 'message']
        widgets = {
            'name': forms.TextInput(attrs={**_INPUT, 'placeholder': 'Ej: Cierre mensual para administración'}),
            'is_active': forms.CheckboxInput(attrs=_CHECK),
            'frequency': forms.Select(attrs=_SELECT),
            'month_day': forms.NumberInput(attrs={**_INPUT, 'min': 1, 'max': 31}),
            'send_time': forms.TimeInput(attrs={**_INPUT, 'type': 'time'}, format='%H:%M'),
            'period': forms.Select(attrs=_SELECT),
            'period_days': forms.NumberInput(attrs={**_INPUT, 'min': 1, 'max': 366}),
            'attach_excel': forms.CheckboxInput(attrs=_CHECK),
            'recipients': forms.Textarea(attrs={**_INPUT, 'rows': 3,
                                                'placeholder': 'gerencia@empresa.cl, contabilidad@empresa.cl'}),
            'subject': forms.TextInput(attrs=_INPUT),
            'message': forms.Textarea(attrs={**_INPUT, 'rows': 3}),
        }
        help_texts = {
            'month_day': 'Si el mes tiene menos días, se envía el último día del mes.',
            'recipients': 'Separados por coma, punto y coma o uno por línea.',
            'subject': 'Por defecto, el nombre del envío. Siempre se le agrega el período del informe.',
            'message': 'Opcional: texto que va al inicio del correo, antes del resumen.',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        names = shift_names()
        current = self.instance.shift_name
        if current and current not in names:
            names.append(current)    # un turno que ya no tiene datos sigue siendo elegible
        self.fields['shift_name'].choices = [('', 'Todos los turnos')] + [(n, n) for n in names]
        self.fields['send_time'].input_formats = ['%H:%M', '%H:%M:%S']
        # solo aplican con frecuencia mensual / período «últimos N días» (ver clean)
        self.fields['month_day'].required = False
        self.fields['period_days'].required = False
        if not self.is_bound:
            self.initial['weekdays'] = [str(i) for i in self.instance.weekdays]

    def clean_recipients(self):
        addresses = split_recipients(self.cleaned_data['recipients'])
        if not addresses:
            raise ValidationError('Indica al menos un destinatario.')
        invalid = []
        for addr in addresses:
            try:
                validate_email(addr)
            except ValidationError:
                invalid.append(addr)
        if invalid:
            raise ValidationError(f'Direcciones no válidas: {", ".join(invalid)}')
        if len(addresses) > MAX_RECIPIENTS:
            raise ValidationError(f'Como máximo {MAX_RECIPIENTS} destinatarios por envío.')
        return ', '.join(addresses)

    def clean_name(self):
        return ' '.join(self.cleaned_data['name'].split())

    def clean(self):
        cleaned = super().clean()
        frequency = cleaned.get('frequency')
        if frequency == ScheduledReport.Frequency.WEEKLY and not cleaned.get('weekdays'):
            self.add_error('weekdays', 'Marca al menos un día de la semana.')
        if frequency == ScheduledReport.Frequency.MONTHLY and not cleaned.get('month_day'):
            self.add_error('month_day', 'Indica el día del mes.')
        if cleaned.get('period') == ScheduledReport.Period.LAST_DAYS and not cleaned.get('period_days'):
            self.add_error('period_days', 'Indica cuántos días cubre el informe.')
        return cleaned

    def save(self, commit=True):
        obj = super().save(commit=False)
        mask = 0
        for d in self.cleaned_data.get('weekdays') or []:
            mask |= 1 << int(d)
        if mask:
            obj.weekdays_mask = mask
        if not obj.month_day:
            obj.month_day = 1
        if not obj.period_days:
            obj.period_days = 7
        # cualquier cambio (horario, activar/pausar) recalcula el próximo envío desde ahora
        obj.schedule_next_run()
        if commit:
            obj.save()
        return obj
