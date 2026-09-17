from django import forms
from django.contrib.auth import get_user_model

from backend.apps.core.models import Station

User = get_user_model()

_INPUT = {'class': 'form-control'}
_SELECT = {'class': 'form-select'}
_CHECK = {'class': 'form-check-input'}


class UserForm(forms.ModelForm):
    """Alta/edición de usuarios del backoffice. La contraseña es opcional al editar."""

    password = forms.CharField(
        label='Contraseña', required=False,
        widget=forms.PasswordInput(attrs={**_INPUT, 'autocomplete': 'new-password'}),
        help_text='Déjala en blanco para no cambiarla (al editar).',
    )

    class Meta:
        model = User
        fields = ['email', 'first_name', 'last_name', 'role', 'is_active']
        widgets = {
            'email': forms.EmailInput(attrs=_INPUT),
            'first_name': forms.TextInput(attrs=_INPUT),
            'last_name': forms.TextInput(attrs=_INPUT),
            'role': forms.Select(attrs=_SELECT),
            'is_active': forms.CheckboxInput(attrs=_CHECK),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is None:
            self.fields['password'].required = True
            self.fields['password'].help_text = 'Requerida para el nuevo usuario.'

    def save(self, commit=True):
        user = super().save(commit=False)
        password = self.cleaned_data.get('password')
        if password:
            user.set_password(password)
        # is_staff (acceso al panel /admin de Django) lo deriva User.save() del rol
        if commit:
            user.save()
        return user


class StationForm(forms.ModelForm):
    """Alta/edición de estaciones. La contraseña de enrolado se guarda hasheada."""

    enroll_password = forms.CharField(
        label='Contraseña de enrolado', required=False,
        widget=forms.PasswordInput(attrs={**_INPUT, 'autocomplete': 'new-password'}),
        help_text='Se ingresa en el terminal junto al ID de dispositivo. '
                  'Déjala en blanco para no cambiarla (al editar).',
    )

    class Meta:
        model = Station
        fields = ['name', 'device_id', 'location', 'is_active']
        widgets = {
            'name': forms.TextInput(attrs=_INPUT),
            'device_id': forms.NumberInput(attrs={**_INPUT, 'min': 0}),
            'location': forms.TextInput(attrs=_INPUT),
            'is_active': forms.CheckboxInput(attrs=_CHECK),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk is None:
            self.fields['enroll_password'].required = True
            self.fields['enroll_password'].help_text = (
                'Requerida: se ingresa en el terminal (junto al ID) para enrolarlo.'
            )

    def save(self, commit=True):
        station = super().save(commit=False)
        password = self.cleaned_data.get('enroll_password')
        if password:
            station.set_enroll_password(password)
        if commit:
            station.save()
        return station


# =====================================================================
#  Configuración compartida con el terminal: turnos/empresas y tarjetas de visita
# =====================================================================
class ShiftScheduleForm(forms.Form):
    """
    Turno programado con sus reglas de autorización. Las empresas se ofrecen como
    casillas (las conocidas por la estación desde la ficha de personas) y además se
    pueden escribir otras separadas por coma (empresas que aún no aparecen en la ficha).
    """

    name = forms.CharField(label='Nombre del turno', max_length=120,
                           widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Ej: Almuerzo'}))
    start = forms.TimeField(label='Inicio', input_formats=['%H:%M', '%H:%M:%S'],
                            widget=forms.TimeInput(attrs={**_INPUT, 'type': 'time'}, format='%H:%M'))
    end = forms.TimeField(label='Término', input_formats=['%H:%M', '%H:%M:%S'],
                          widget=forms.TimeInput(attrs={**_INPUT, 'type': 'time'}, format='%H:%M'))
    enabled = forms.BooleanField(label='Habilitado', required=False, initial=True,
                                 widget=forms.CheckboxInput(attrs=_CHECK))
    days = forms.MultipleChoiceField(
        label='Días en que se sirve', required=False,
        choices=[(str(i), n) for i, n in enumerate(
            ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'])],
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
        help_text='El turno solo se puede iniciar en el terminal los días marcados.',
    )
    allow_visitors = forms.BooleanField(label='Admite visitas (tarjeta RFID)', required=False, initial=True,
                                        widget=forms.CheckboxInput(attrs=_CHECK))
    is_lunch = forms.BooleanField(
        label='Es el turno de almuerzo', required=False, initial=False,
        widget=forms.CheckboxInput(attrs=_CHECK),
        help_text='Las personas con colación «solo almuerzo» (campo Colacion = 1 en HikCentral) '
                  'solo pueden retirar en los turnos marcados como almuerzo.',
    )
    all_companies = forms.BooleanField(
        label='Todas las empresas', required=False, initial=True,
        widget=forms.CheckboxInput(attrs=_CHECK),
        help_text='Desmarca para autorizar solo a las empresas elegidas; el resto queda NO AUTORIZADO en este turno.',
    )
    companies = forms.MultipleChoiceField(
        label='Empresas autorizadas', required=False,
        widget=forms.CheckboxSelectMultiple(attrs={'class': 'form-check-input'}),
    )
    other_companies = forms.CharField(
        label='Otras empresas (separadas por coma)', required=False,
        widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Ej: Constructora Andes, Aseo Industrial SpA'}),
        help_text='Para empresas que aún no aparecen en la ficha de personas de la estación.',
    )

    def __init__(self, *args, station=None, instance=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.station = station
        self.instance = instance
        known = list(station.known_companies()) if station else []
        current = instance.company_names if instance else []
        seen, choices = set(), []
        for c in known + current:
            key = (c or '').strip().upper()
            if key in seen:
                continue
            seen.add(key)
            name = (c or '').strip()
            choices.append((name, name or '(Sin empresa)'))
        # "(Sin empresa)" al final
        choices.sort(key=lambda t: (t[0] == '', t[1].upper()))
        self.fields['companies'].choices = choices
        if instance is not None and not self.is_bound:
            self.initial.update({
                'name': instance.name,
                'start': instance.start_text,
                'end': instance.end_text,
                'enabled': instance.enabled,
                'days': [str(i) for i in range(7) if instance.days_mask & (1 << i)],
                'allow_visitors': instance.allow_visitors,
                'is_lunch': instance.is_lunch,
                'all_companies': instance.all_companies,
                'companies': [c for c in current],
            })

    def cleaned_companies(self):
        """Empresas autorizadas: casillas + las escritas a mano, sin repetir."""
        out, seen = [], set()
        for c in list(self.cleaned_data.get('companies') or []):
            key = c.strip().upper()
            if key not in seen:
                seen.add(key)
                out.append(c.strip())
        for c in (self.cleaned_data.get('other_companies') or '').split(','):
            c = c.strip()
            if c and c.upper() not in seen:
                seen.add(c.upper())
                out.append(c)
        return out

    @staticmethod
    def _to_min(t):
        return t.hour * 60 + t.minute

    def clean_days(self):
        """Sin ningún día marcado el turno no se podría iniciar nunca: se asumen todos."""
        return self.cleaned_data.get('days') or [str(i) for i in range(7)]

    def days_mask(self):
        mask = 0
        for d in self.cleaned_data.get('days') or []:
            mask |= 1 << int(d)
        return mask or 0b1111111

    def save(self, station):
        from backend.apps.core.models import ShiftSchedule, ShiftScheduleCompany
        d = self.cleaned_data
        obj = self.instance or ShiftSchedule(station=station)
        obj.name = d['name'].strip()
        obj.start_min = self._to_min(d['start'])
        obj.end_min = self._to_min(d['end'])
        obj.enabled = d['enabled']
        obj.days_mask = self.days_mask()
        obj.allow_visitors = d['allow_visitors']
        obj.is_lunch = d['is_lunch']
        obj.all_companies = d['all_companies']
        obj.save()
        obj.companies.all().delete()
        for c in self.cleaned_companies():
            ShiftScheduleCompany.objects.create(schedule=obj, company=c)
        station.touch_config()
        return obj


class ShiftOvertimeForm(forms.ModelForm):
    """Prórroga de cierre de turno (parte de la configuración compartida con el terminal)."""

    class Meta:
        model = Station
        fields = ['shift_overtime_minutes']
        widgets = {
            'shift_overtime_minutes': forms.NumberInput(
                attrs={**_INPUT, 'min': 1, 'max': 180, 'class': 'form-control form-control-sm'},
            ),
        }

    def clean_shift_overtime_minutes(self):
        minutes = self.cleaned_data['shift_overtime_minutes']
        if not 1 <= minutes <= 180:
            raise forms.ValidationError('La prórroga debe estar entre 1 y 180 minutos.')
        return minutes

    def save(self, commit=True):
        station = super().save(commit=commit)
        if commit:
            # la marca de tiempo hace que el terminal adopte este valor en su próxima sync
            station.touch_config()
        return station


class VisitorCardForm(forms.Form):
    """Alta de una tarjeta del set de visitas."""

    card_no = forms.CharField(label='Número de tarjeta', max_length=100,
                              widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Tal como lo entrega el lector'}))
    label = forms.CharField(label='Etiqueta', max_length=120, required=False,
                            widget=forms.TextInput(attrs={**_INPUT, 'placeholder': 'Ej: Visita 01'}))
    enabled = forms.BooleanField(label='Habilitada', required=False, initial=True,
                                 widget=forms.CheckboxInput(attrs=_CHECK))

    def clean_card_no(self):
        from backend.apps.core.models import VisitorCard
        no = VisitorCard.normalize(self.cleaned_data['card_no'])
        if not no:
            raise forms.ValidationError('Ingresa el número de la tarjeta.')
        return no
