# Integración Casino — Backend

Backoffice en Django + PostgreSQL (Docker) que respalda los registros de colaciones
producidos por la app de escritorio **CasinoAccess** (C#/Avalonia). Si una estación
(máquina) falla, los datos quedan a salvo en el servidor.

Incluye:

- **API de ingesta** con autenticación por API key para que cada estación suba sus
  registros (personas, turnos y marcaciones) de forma idempotente.
- **Login y administración de usuarios** del backoffice (roles: administrador,
  operador, solo lectura).
- **Reportería** de colaciones (diaria / mensual / rango libre) con exportación a
  **PDF** y **Excel**, replicando el informe de la solución C#.

---

## Puesta en marcha (Docker)

```bash
cd integracion_casino_backend
cp variables.env.example variables.env   # completar credenciales y secret key
docker compose up --build
```

`variables.env` contiene secretos y está en `.gitignore`; no se versiona.

Servicios:

| Servicio | Descripción            | Puerto host |
|----------|------------------------|-------------|
| `web`    | Django (backoffice+API)| `8010`      |
| `pgdb`   | PostgreSQL 16          | `5433`      |

El contenedor `web` en el arranque: espera la DB, aplica migraciones, recolecta
estáticos y crea el superusuario inicial (`ensure_superuser`).

Accede a:

- Backoffice: <http://localhost:8010/>
- Admin Django: <http://localhost:8010/admin/>
- Healthcheck: <http://localhost:8010/api/healthcheck/>

Credenciales iniciales: las definidas en `DJANGO_SUPERUSER_EMAIL` /
`DJANGO_SUPERUSER_PASSWORD` de `variables.env`.

> Cambia `DJANGO_SUPERUSER_*`, `DJANGO_SECRET_KEY` y las credenciales de Postgres
> antes de usar en producción, y pon `DEBUG=0` (arranca con Gunicorn).

---

## Conectar una estación (CasinoAccess)

1. En el backoffice → **Estaciones** → *Nueva estación*.
2. En el detalle de la estación → *Generar* API key. **Cópiala en ese momento**
   (solo se muestra una vez).
3. La app debe llamar al endpoint enviando la clave en la cabecera:

```
POST /api/sync/
Authorization: Api-Key <clave>
Content-Type: application/json

{
  "persons": [
    {"employee_no": "E1", "name": "Ana Pérez", "company": "ACME",
     "authorized": true, "person_id": "", "user_type": "normal"}
  ],
  "shifts": [
    {"remote_id": 1, "uid": "9f2c…", "name": "Almuerzo",
     "started_at": "2026-07-10T12:00:00", "ended_at": "2026-07-10T14:00:00",
     "auto": true, "schedule_uid": "7ab1…", "service_date": "2026-07-10",
     "end_reason": "reemplazado", "reopened_from_uid": ""}
  ],
  "events": [
    {"remote_id": 10, "uid": "c41e…", "shift_uid": "9f2c…", "shift_remote_id": 1,
     "employee_no": "E1", "person_name": "Ana Pérez", "company": "ACME",
     "verify_method": "face", "card_no": "",
     "event_time": "2026-07-10T12:05:00", "status": "Ok"}
  ]
}
```

- Las horas van en **hora local** (formato ISO `YYYY-MM-DDTHH:MM:SS`), tal como las
  guarda el SQLite de la app.
- `uid` es la **identidad estable** de cada turno y marcación: sobrevive a que se recree la
  base local del terminal (los `remote_id` se reinician y pisarían historia). La ingesta es
  **idempotente** por `(estación, uid)`: reenviar el mismo lote actualiza, no duplica. Un
  terminal que no envíe `uid` se identifica por `remote_id`, como antes; al llegar después
  con `uid`, el registro existente lo adopta en vez de duplicarse.
- Un turno describe **una apertura concreta**: `schedule_uid` la liga al turno programado del
  que nació (sobrevive a renombres), `service_date` es su día operacional (un turno que cruza
  medianoche pertenece al día en que empezó), `end_reason` ∈ `manual`, `reemplazado`,
  `expirado`, `interrumpido`, y `reopened_from_uid` apunta a la apertura original cuando el
  turno se reabrió porque quedó un comensal fuera.
- `status` ∈ `Ok`, `Duplicado`, `SinTurno`, `NoAutorizado`. Los eventos aceptan además
  `is_visitor` (marcación de visita con tarjeta RFID), `detail` (motivo del estado) y
  `photo_b64` (foto en base64, **solo en marcaciones de visita**: son pagos adicionales y la
  foto es la constancia). Una foto ilegible se descarta sin rechazar el lote.
- Los tres arreglos son opcionales; puedes enviar solo `events`, o todo junto.
- **`config`** (opcional): configuración compartida —turnos programados con empresas
  autorizadas y set de tarjetas de visita— con su marca de edición `updated_at` (UTC, ms).
  El servidor la compara con `Station.config_updated_at`: si la del terminal es más nueva,
  reemplaza la del servidor; si la del servidor es más nueva (editada en Turnos y empresas /
  Tarjetas de visita), la respuesta incluye `config` para que el terminal la aplique.
  Incluye `shift_overtime_minutes`: los minutos que un turno sigue abierto tras su horario
  cuando no viene otro turno detrás. El `uid` de cada turno programado también viaja aquí, y
  es lo que permite ligar cada apertura con su definición.

```json
"config": {
  "updated_at": "2026-08-17T15:04:05.123Z",
  "shift_overtime_minutes": 10,
  "schedules": [
    {"uid": "7ab1…", "name": "Almuerzo", "start_min": 720, "end_min": 840, "enabled": true,
     "all_companies": false, "allow_visitors": true, "companies": ["Casino Central", ""]}
  ],
  "visitor_cards": [
    {"card_no": "0012345678", "label": "Visita 01", "enabled": true, "created_at": "2026-08-17T10:00:00"}
  ]
}
```

Respuesta:

```json
{"validate": true, "message": "Sincronización realizada",
 "result": {"persons": {"created": 1, "updated": 0},
            "shifts":  {"created": 1, "updated": 0},
            "events":  {"created": 1, "updated": 0}},
 "config": { ... solo si la del servidor es más nueva ... }}
```

---

## Roles y accesos

El backoffice tiene tres roles. **Estar autenticado no da acceso a nada por sí solo**: cada
vista exige una capacidad y lo que no está permitido se niega.

| Rol | Panel | Monitor en vivo | Colaciones de visitas | Reportería | Configuración |
|---|---|---|---|---|---|
| **Administrador del sistema** (`admin`) | Sí | Sí | Sí | Sí | Sí |
| **Gerente de administración** (`gerente`) | Sí | Sí | Sí | Sí | No |
| **Personal del casino** (`casino`) | No | Sí | Sí | Sí | No |

«Configuración» son turnos y empresas, estaciones (con sus API keys) y usuarios. El panel
`/admin/` de Django queda solo para el administrador: `is_staff` lo deriva `User.save()` del
rol, así que bajarle el rol a alguien le cierra también esa puerta.

Detalles de implementación (`backend/apps/webapp/permissions.py`):

- Las capacidades se declaran una sola vez (`is_admin`, `can_see_tickets`,
  `can_see_dashboard` en `User`) y los decoradores `admin_required` / `tickets_required`
  las aplican. El menú lateral se dibuja con las mismas propiedades, así que no se ofrece
  lo que la vista va a rechazar.
- Al negar se redirige a la sección de entrada del propio usuario, nunca a una página que
  tampoco pueda ver (eso haría un bucle). Un rol sin ninguna sección permitida recibe 403.
- La raíz `/` es el panel y actúa de entrada: al personal del casino lo lleva al monitor sin
  mostrarle un error de permisos.
- El WebSocket del monitor exige el mismo permiso que la página: por ahí viajan las mismas
  marcaciones, así que pedir solo sesión sería una puerta de atrás.
- El `next` del login se valida contra el propio host, para que un enlace preparado no saque
  al usuario del backoffice justo después de escribir su contraseña.

La matriz completa está cubierta por pruebas en `backend/apps/webapp/tests.py`: recorren
todas las rutas para los tres roles, de modo que una vista nueva sin proteger se detecta al
sumarla a la lista.

Los roles antiguos `operator` y `viewer` desaparecieron; la migración `0008` pasa a sus
usuarios a `casino`, el más restringido.

---

## Colaciones de visitas

Las colaciones que retiran las visitas con tarjeta RFID **se facturan aparte**, así que
tienen su propia pantalla de control (menú **Colaciones de visitas**, `/visitas/`).

Cada marcación se muestra con la **foto que el terminal tomó al momento de retirar** —la
constancia de quién comió—, la tarjeta usada, el turno (marcado si fue una reapertura), la
estación y el estado. Se filtra por rango de fechas, estación, estado y número o etiqueta de
tarjeta, y hay un resumen de colaciones cobrables por tarjeta.

Solo cuenta como cobrable el estado **Se cobra** (`Ok`). Una **repetida** es la misma tarjeta
pasando dos veces en el mismo servicio —incluidas las reaperturas del turno— y no corresponde
facturarla.

Las fotos se sirven en `/visitas/<id>/foto/`, solo para marcaciones de visita y solo a
usuarios autenticados.

---

## Cierre de turnos

En **Turnos y empresas** se define la **prórroga**: los minutos que un turno sigue abierto
después de su hora de término cuando **no hay otro turno a continuación**. Treinta segundos
antes del cierre, el terminal pregunta en pantalla si se extiende; sin respuesta se cierra
solo. Si en cambio empieza el turno siguiente, el turno en curso se cierra de inmediato y
queda registrado como `reemplazado`.

El valor se edita aquí o en el terminal (Configuración → Turnos) y viaja en la configuración
compartida: gana la edición más reciente.

---

## Reportería

En **Reportería** eliges rango de fechas y estación (o todas). El informe replica
el PDF de la app C#:

- **Resumen**: colaciones servidas, personas distintas, intentos duplicados,
  no autorizados, fuera de turno sin asociar.
- **Por día**, **por turno**, **por empresa** y **detalle por persona**.

"Colaciones servidas" = marcaciones `Ok` **más** las `SinTurno` atribuidas a un turno
mediante la ventana de gracia (`REPORT_GRACE_MINUTES`, por defecto 15 min — debe
coincidir con la app). Botones **PDF** y **Excel** para descargar.

Para el cierre de mes: entra con el rango del primer al último día del mes; el título
del informe se rotula automáticamente como "Informe mensual — <mes> <año>".

---

## Desarrollo local sin Docker

```bash
python -m venv .venv && .venv/Scripts/activate     # Windows
pip install -r requirements/common.txt
# DB rápida en SQLite (solo desarrollo):
export DB_ENGINE=sqlite DJANGO_SECRET_KEY=dev DEBUG=1
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

En producción/Docker se usa PostgreSQL (variables `POSTGRES_*`, `HOST_DB`, `PORT_DB`).

---

## Estructura

```
backend/
  settings.py, urls.py, wsgi.py, asgi.py
  apps/
    core/            Modelos: User, Station, StationAPIKey, Person, Shift, AccessEvent
    authentication/  Login por email
    api/             Ingesta con API key (/api/sync/, /api/healthcheck/)
    webapp/          Dashboard, usuarios, estaciones y API keys
    reports/         Servicio de informe + generadores PDF/Excel
  templates/         Plantillas (Bootstrap 5)
docker/              Dockerfiles de web y postgresql
requirements/        Dependencias
```
