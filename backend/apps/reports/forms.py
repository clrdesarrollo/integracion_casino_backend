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


class AnnulEventForm(forms.Form):
    reason = forms.CharField(label='Motivo de la anulación', min_length=5, max_length=300,
                             widget=forms.TextInput(attrs=_INPUT))

    def clean_reason(self):
        return ' '.join(self.cleaned_data['reason'].split())


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
