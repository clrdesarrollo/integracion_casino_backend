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
- `status` ∈ `Ok`, `Duplicado`, `SinTurno`, `NoAutorizado` (`Anulado` existe solo en el
  servidor, para ingresos manuales anulados: el terminal no lo manda). Los eventos aceptan además
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

Los roles se crean y editan desde el propio backoffice (menú **Roles**, requiere el permiso
«Usuarios y roles»). Un rol es un conjunto de permisos; a cada usuario se le asigna uno.
**Estar autenticado no da acceso a nada por sí solo**: cada vista exige un permiso y lo que no
está marcado se niega.

Permisos disponibles (catálogo en `backend/apps/core/access.py`):

| Permiso | Da acceso a |
|---|---|
| Panel | resumen de la operación |
| Monitor en vivo | monitor y su WebSocket |
| Reportería | informes y descargas PDF/Excel |
| Registro de visitas | entregar y recibir tarjetas de visita |
| Colaciones de visitas | marcaciones con tarjeta de visita y su foto |
| Administrar visitas | eliminar registros de visita e inventario de tarjetas |
| Turnos, empresas y estaciones | configuración compartida con el kiosco, estaciones y API keys |
| Usuarios y roles | alta de usuarios y definición de roles |
| Ingreso manual de colaciones | registrar a mano una colación que el kiosco no pudo marcar (y anularla) |
| Bitácora | todo lo que pasa en el sistema, en orden de tiempo |

Vienen tres roles de sistema (se pueden ajustar, no eliminar): **Administrador del sistema**
(`admin`, acceso total y fijo), **Gerente de administración** (`gerente`) y **Personal del
casino** (`casino`, el rol de partida de un usuario nuevo por ser el más restringido).

Reglas:

- El administrador siempre tiene todos los permisos y es el único rol con acceso al panel
  `/admin` de Django (`is_staff` lo deriva `User.save()` del rol). No se puede dejar al sistema
  sin un administrador activo (ni quitándole el rol, ni desactivándolo, ni eliminándolo).
- Un rol con usuarios asignados no se puede eliminar.
- Los decoradores son `capability_required('<permiso>')` (`webapp/permissions.py`); el menú
  lateral usa `user.caps.<permiso>`, así que no se ofrece lo que la vista va a rechazar.
  Para sumar una sección: declarar el permiso en `core/access.py` y protegerla con el decorador;
  el formulario de roles lo ofrece solo.
- Al negar se redirige a la primera sección que el usuario sí puede ver, nunca a una página que
  tampoco pueda ver (haría un bucle). Un rol sin ningún permiso recibe 403.
- El WebSocket del monitor exige el mismo permiso que la página y lo revalida durante la conexión.
- El `next` del login se valida contra el propio host.

Las pruebas (`backend/apps/webapp/tests.py`) recorren todas las rutas por rol y cubren los roles
personalizados. Los roles antiguos `operator` y `viewer` desaparecieron (migración `0008`); la
`0012`–`0014` pasan el rol de texto a la tabla de roles sin tocar a los usuarios.

---

## Visitas

El menú **Visitas** (`/visitas/`) reúne todo lo de las tarjetas de visita en tres pestañas.

**Registro de visitas** (`/visitas/`, los tres roles): a quién se entrega cada tarjeta física,
quién la entrega (el usuario que registra) y a quién viene a ver, eligiendo empresa y funcionario
(autocompletado por nombre desde la ficha de personas sincronizada de HikCentral; si el nombre
no coincide se guarda igual como texto). Las colaciones que se retiren con esa tarjeta se
atribuyen a la visita **hasta que se registre la devolución**; si se vuelve a entregar una
tarjeta «en uso», la visita anterior se cierra sola en ese momento. El registro vive en
`tb_visit` y guarda la tarjeta por número (el set se reemplaza completo al sincronizar con el
terminal) y el nombre del funcionario en texto (la ficha puede desaparecer al resincronizar).
Solo el administrador puede borrar un registro equivocado.

**Tarjetas** (`/visitas/tarjetas/`, solo administrador): el inventario de tarjetas físicas por
estación. Es la lista con la que el terminal reconoce que una tarjeta es de visita (foto,
insignia VISITA, una colación por tarjeta por turno, facturación aparte). **No habilita la
tarjeta en el control de acceso**: quien la lee es el equipo Hikvision, así que además debe
estar dada de alta en HikCentral con su nivel de acceso. Una tarjeta que el equipo acepta pero
no está en el inventario se cuenta como colación de la persona a la que HikCentral se la
asignó, no como visita.

**Colaciones** (`/visitas/colaciones/`): las colaciones que retiran las visitas con tarjeta RFID
**se facturan aparte**, así que tienen su propia pantalla de control. Junto a cada marcación se
muestra a quién se le había entregado la tarjeta en ese momento; si nadie registró la entrega
aparece como «sin registro» (se cobra igual).

Cada marcación se muestra con la **foto que el terminal tomó al momento de retirar** —la
constancia de quién comió—, la tarjeta usada, el turno (marcado si fue una reapertura), la
estación y el estado. Se filtra por rango de fechas, estación, estado y número o etiqueta de
tarjeta, y hay un resumen de colaciones cobrables por tarjeta.

Solo cuenta como cobrable el estado **Se cobra** (`Ok`). Una **repetida** es la misma tarjeta
pasando dos veces en el mismo servicio —incluidas las reaperturas del turno— y no corresponde
facturarla.

Las fotos se sirven en `/visitas/<id>/foto/`, solo para marcaciones de visita y solo a
usuarios autenticados.

En la **reportería** (pantalla, PDF y Excel) hay una sección **Visitas** con cada visita del
período —entregada, visita, a quién venía a ver, tarjeta, quién la entregó, devolución y
colaciones retiradas— más las tarjetas usadas sin registro; en el detalle por persona la visita
registrada figura con su nombre.

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
  no autorizados, fuera de turno.
- **Por día**, **por turno**, **por empresa** y **detalle por persona**.

"Colaciones servidas" = marcaciones `Ok` **más** las de ingreso manual. Las marcaciones
fuera de turno (`SinTurno`) no suman: el kiosco las rechaza y no se sirve colación.
Botones **PDF** y **Excel** para descargar.

Para el cierre de mes: entra con el rango del primer al último día del mes; el título
del informe se rotula automáticamente como "Informe mensual — <mes> <año>".

### Envíos por correo

En **Informes → Envíos por correo** se programa el envío automático del informe en **PDF**
(y opcionalmente también en Excel) a una lista de correos. Cada envío define:

- **Cuándo**: todos los días, ciertos días de la semana o un día del mes (si el mes es más
  corto, sale el último día), a una hora.
- **Qué período cubre**: día anterior, mismo día, semana anterior (lunes a domingo), mes
  anterior, últimos N días, o **desde el envío anterior** (p. ej. mensual el día 24 → del 24
  del mes anterior al 23).
- Estación y turno (o todos), destinatarios, asunto y un mensaje opcional. El correo trae un
  resumen con las cifras principales.

«Enviar ahora» manda en el momento el período que cubriría un envío hecho hoy. El
**historial** guarda cada envío (período, destinatarios, estado, error); se conserva aunque se
elimine el envío programado.

En **Informes → Servidor de correo** se configura la cuenta SMTP (servidor, puerto,
STARTTLS / SSL / sin cifrado, usuario, remitente) y se envía un correo de prueba. La
contraseña se guarda **cifrada** con una clave derivada de `DJANGO_SECRET_KEY`: si esa clave
cambia, hay que volver a ingresarla (la pantalla lo avisa).

Permisos: **Envíos por correo** (`report_mail`) y **Servidor de correo** (`mail_server`). De
partida solo los tiene el administrador; se asignan a otros roles en **Roles**. Quien puede
programar envíos puede mandar el informe a cualquier dirección.

**El programador** es el servicio `scheduler` de docker-compose (misma imagen que `web`):

```bash
python manage.py send_scheduled_reports --loop   # servicio: revisa cada 30 s
python manage.py send_scheduled_reports          # un solo ciclo (cron, pruebas)
```

Cuando a un envío le llega la hora se encola con su período y se manda. Si el servidor de
correo falla, se reintenta a los 10 y a los 30 minutos; tras el tercer intento queda
«Falló» y se puede reintentar a mano desde el historial. Si el programador estuvo detenido y
pasaron varias fechas, solo se manda la más reciente. Las horas son las de `TIME_ZONE`.
Si el servicio no está corriendo, Envíos por correo y Servidor de correo lo advierten.

---

## Ingreso manual de colaciones

Cuando el kiosco no pudo registrar colaciones que sí se sirvieron (un corte de energía lo
apagó, por ejemplo), se ingresan a mano en **Informes → Ingresos manuales** o con el botón
**Ingresar colación** del detalle de un turno. Requiere el permiso **Ingreso manual de
colaciones** (`manual_events`), que de partida solo tiene el administrador.

Reglas, pensadas para que un error no ensucie la información:

- Se registra **sobre el turno** en que se sirvió, y solo sobre turnos **cerrados** con
  marcaciones (no sobre un turno en curso, que el terminal sigue administrando, ni sobre
  un registro de ingreso manual de la cocinera).
- La persona se **busca en la ficha de HikCentral** por nombre (sin importar tildes) o por
  RUT (con o sin puntos, guion y dígito verificador); no se escriben nombres a mano.
- **Una colación por persona y servicio**: si ya tiene una válida en el turno o en una
  reapertura del mismo, se bloquea.
- La hora debe caer dentro del turno (hasta su término programado, si el terminal lo cerró
  antes por haberse caído).
- Lo que el terminal habría rechazado —persona no autorizada, «sin colación», «solo
  almuerzo» en otro turno, empresa fuera del turno— se avisa y exige marcar «Registrar de
  todos modos»; queda anotado en la bitácora.
- Motivo y confirmación obligatorios. La marcación nace con origen `backoffice`, quién la
  registró y cuándo, y método «Manual (backoffice)».
- **No se edita ni se borra**: si estuvo mal, se **anula** con motivo; la fila queda
  (estado `Anulado`) y deja de contar.

En el detalle del turno, en el informe (pantalla, PDF y Excel) y en el Excel del turno la
colación figura marcada como ingreso manual, con quién la registró y por qué. El informe
trae el indicador «Ingresadas a mano», la columna «A mano» por turno y una sección con el
detalle; en los totales por empresa y por persona cuenta como cualquier otra servida.

---

## Bitácora

El menú **Bitácora** (`/bitacora/`, permiso `audit`, de partida solo el administrador) es el
registro de todo lo que pasa, a la hora real del hecho, con filtros por período, categoría,
estación, nivel y texto. Las entradas no se editan ni se borran (tampoco desde `/admin`).

| Categoría | Qué anota |
|---|---|
| Terminal | se conectó o se desconectó del canal de órdenes (`/ws/station/`), volvió a sincronizar tras más de 15 min sin contacto, se enroló, y cada **reinicio** con la última hora en que la app estuvo viva (lo informa el propio terminal) |
| Turnos | cada apertura y cierre tal como los informa el terminal, con el motivo; un cierre «interrumpido» queda como aviso |
| Colaciones | ingresos manuales y anulaciones, con persona, hora, motivo y avisos ignorados |
| Visitas | tarjeta entregada, devuelta, registro eliminado |
| Configuración | estaciones, API keys, turnos programados (antes/después), prórroga, modo de pruebas, turnos automáticos, tarjetas de visita, actualización de personas |
| Usuarios y roles | altas, cambios de rol/estado/contraseña, bajas; permisos agregados y quitados a cada rol |
| Sesiones | inicios, intentos fallidos (solo el correo) y cierres de sesión |
| Correo | servidor guardado, envíos programados y «enviar ahora» |

Para reconstruir un incidente como un corte de energía: filtra por **Terminal** y verás a
qué hora se desconectó el kiosco, cuándo arrancó de nuevo y desde cuándo estaba caído; en
**Turnos**, el cierre interrumpido; y en **Colaciones**, lo que se ingresó a mano después.

El terminal manda los reinicios en la sincronización (`incidents`, idempotentes por `uid`):

```json
"incidents": [
  {"kind": "restart", "uid": "9a1f…", "at": "2026-10-07T09:01:50",
   "last_alive_at": "2026-10-07T07:41:10"}
]
```

Desde esta versión el kiosco guarda un **latido** cada 30 s; con él, un turno que quedó
abierto cuando la app se cayó se cierra como «interrumpido» a la **última hora en que la
app estuvo viva**, no a la hora en que la volvieron a encender.

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
    reports/         Servicio de informe + generadores PDF/Excel + envíos por correo
  templates/         Plantillas (Bootstrap 5)
docker/              Dockerfiles de web y postgresql
requirements/        Dependencias
```
