from django import forms


class LoginForm(forms.Form):
    email = forms.EmailField(
        label='Correo',
        widget=forms.EmailInput(attrs={
            'class': 'form-control', 'placeholder': 'correo@empresa.cl', 'autofocus': True,
        }),
    )
    password = forms.CharField(
        label='Contraseña',
        widget=forms.PasswordInput(attrs={
            'class': 'form-control', 'placeholder': '••••••••',
        }),
    )
