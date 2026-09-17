#!/bin/sh
set -e

echo "==> Esperando a PostgreSQL en ${HOST_DB}:${PORT_DB}..."
until python -c "import socket,os,sys; s=socket.socket(); s.settimeout(2); \
    s.connect((os.environ['HOST_DB'], int(os.environ['PORT_DB']))); s.close()" 2>/dev/null; do
    echo "   ...db no disponible aún, reintentando"
    sleep 2
done
echo "==> PostgreSQL disponible."

echo "==> Aplicando migraciones..."
python manage.py migrate --noinput

echo "==> Recolectando archivos estáticos..."
python manage.py collectstatic --noinput

echo "==> Asegurando superusuario inicial..."
python manage.py ensure_superuser || true

if [ "${DEBUG}" = "1" ]; then
    echo "==> Iniciando servidor de desarrollo (runserver, ASGI vía daphne)."
    exec python manage.py runserver 0.0.0.0:8000
else
    echo "==> Iniciando Daphne (ASGI, soporta WebSocket)."
    exec daphne -b 0.0.0.0 -p 8000 backend.asgi:application
fi
