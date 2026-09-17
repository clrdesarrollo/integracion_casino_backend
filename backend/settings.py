"""
Django settings para el backend de Integración Casino (respaldo de colaciones).

Basado en las convenciones del proyecto citofono4g-backend (CLRobotics).
"""

from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent


def to_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on', 't')


# ---- Seguridad ----
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'django-insecure-dev-only-key')
DEBUG = to_bool(os.getenv('DEBUG', '1'))

ALLOWED_HOSTS = ['localhost', '127.0.0.1'] + [
    h for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h
]

CSRF_TRUSTED_ORIGINS = [
    o for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o
]


# ---- Apps ----
INSTALLED_APPS = [
    'daphne',  # debe ir antes de staticfiles para servir ASGI en runserver
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    'channels',
    'rest_framework',
    'rest_framework_api_key',

    'backend.apps.core',
    'backend.apps.authentication',
    'backend.apps.api',
    'backend.apps.webapp',
    'backend.apps.reports',
    'backend.apps.realtime',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'backend.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'backend' / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'backend.wsgi.application'
ASGI_APPLICATION = 'backend.asgi.application'


# ---- Channels (tiempo real) ----
# Con Redis (producción/Docker) o en memoria (desarrollo sin Redis).
if os.getenv('CHANNELS_IN_MEMORY', '0') == '1':
    CHANNEL_LAYERS = {'default': {'BACKEND': 'channels.layers.InMemoryChannelLayer'}}
else:
    CHANNEL_LAYERS = {
        'default': {
            'BACKEND': 'channels_redis.core.RedisChannelLayer',
            'CONFIG': {
                'hosts': [(os.getenv('REDIS_HOST', 'redis'), int(os.getenv('REDIS_PORT', '6379')))],
            },
        },
    }


# ---- Base de datos ----
# Por defecto PostgreSQL. Para pruebas locales rápidas sin Postgres se puede
# usar SQLite exportando DB_ENGINE=sqlite (no usar en producción).
if os.getenv('DB_ENGINE', 'postgres').lower() == 'sqlite':
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': os.getenv('DB_NAME', str(BASE_DIR / 'db.sqlite3')),
        },
    }
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': os.getenv('POSTGRES_DB', 'casino_backend'),
            'USER': os.getenv('POSTGRES_USER', 'CLRadmin'),
            'PASSWORD': os.getenv('POSTGRES_PASSWORD', ''),
            'HOST': os.getenv('HOST_DB', 'localhost'),
            'PORT': os.getenv('PORT_DB', '5432'),
        },
    }


# ---- DRF ----
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DATETIME_FORMAT': '%Y-%m-%dT%H:%M:%S%z',
    # Frena la fuerza bruta contra el enrolado de terminales (ID + contraseña).
    'DEFAULT_THROTTLE_RATES': {
        'enroll': '10/min',
    },
    # Sin esto, DRF identifica al cliente por la cabecera X-Forwarded-For, que el propio
    # cliente elige: rotándola, cada intento caería en un contador distinto y el límite
    # de arriba no serviría de nada. En 0 se usa la IP real de la conexión.
    # Si algún día se pone un proxy inverso delante, hay que subirlo al número de saltos.
    'NUM_PROXIES': 0,
}

# Cabecera que la app CasinoAccess usará: Authorization: Api-Key <clave>
API_KEY_CUSTOM_HEADER = None  # usamos permission propia que lee "Authorization: Api-Key ..."


# ---- Password validation ----
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 8}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]


# ---- i18n / tz ----
LANGUAGE_CODE = 'es-cl'
TIME_ZONE = os.getenv('TIME_ZONE', 'America/Santiago')
USE_I18N = True
USE_TZ = True


# ---- Estáticos y media ----
STATIC_URL = 'static/'
STATICFILES_DIRS = [BASE_DIR / 'backend' / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'
STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage'},
}

MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'backend' / 'media'

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'


# ---- Autenticación ----
AUTH_USER_MODEL = 'core.User'
AUTHENTICATION_BACKENDS = [
    'backend.apps.authentication.backends.EmailBackend',
    'django.contrib.auth.backends.ModelBackend',
]
LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/login/'


# ---- Reportería ----
REPORT_GRACE_MINUTES = int(os.getenv('REPORT_GRACE_MINUTES', '15'))


# ---- Superusuario inicial (comando ensure_superuser) ----
DJANGO_SUPERUSER_EMAIL = os.getenv('DJANGO_SUPERUSER_EMAIL', '')
DJANGO_SUPERUSER_PASSWORD = os.getenv('DJANGO_SUPERUSER_PASSWORD', '')


# ---- Logging ----
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {'class': 'logging.StreamHandler'},
    },
    'root': {
        'handlers': ['console'],
        'level': os.getenv('DJANGO_LOG_LEVEL', 'INFO'),
    },
}

MESSAGE_TAGS = {
    10: 'debug', 20: 'info', 25: 'success', 30: 'warning', 40: 'danger',
}
