# NaftaMon Web — dashboard y backend

Dashboard local para ver alertas activas recibidas desde NaftaMon Desktop. Incluye una interfaz responsive en español, APIs de lectura y recepción de snapshots con persistencia SQLite.

## Requisitos

- Docker Desktop con Compose v2.
- Docker Engine en Linux con Docker Compose v2 también es compatible; la validación de esta entrega se ejecutó en Windows 11 con Docker Desktop.
- Para recibir datos desde otra computadora: ambos equipos en una LAN de confianza y la IP LAN privada del equipo que corre Docker.

## Arquitectura

FastAPI valida los snapshots del Gateway, los reconcilia en SQLite y sirve el dashboard local desde el contenedor `web`. Un contenedor `notifications` usa la misma base para enviar trabajos de la cola; no publica puertos. Uvicorn escucha en `0.0.0.0:8000` dentro del contenedor; Compose publica el puerto del host en `127.0.0.1:8080` por defecto. La base vive en el volumen `naftamon_web_data`. No hay login ni recursos remotos obligatorios para el dashboard o la ingesta. Los destinos de notificación son opcionales y pueden requerir conexión de red.

La página principal en `http://127.0.0.1:8080/` abre directamente el dashboard, sin login. No usa cuentas, sesiones, API keys, tokens, CDN ni recursos remotos. El dashboard y las APIs son accesibles sin autenticación desde cualquier cliente que alcance el puerto. No lo expongas a Internet.

## Dashboard

La vista principal muestra el total de alertas, CRITICAL, WARNING, UNKNOWN y hosts DOWN/UNREACHABLE; cuenta fuentes online/offline por su última recepción y muestra separadamente errores o estados pendientes de servidores Thruk. Una consulta vacía solo se presenta como saludable cuando las fuentes están online y los snapshots son completos y exitosos. Si falta información, la interfaz advierte que la ausencia de alertas no confirma salud.

La tabla presenta únicamente campos que envía el Gateway o que el receptor observa: fuente provisional (nombre e IP peer), servidor, backend, host, servicio, estado, confirmación del dato, acknowledgement, downtime y última recepción. El contrato no envía plugin output ni descripción; por eso no se muestran. Los filtros combinables cubren host, servicio, texto, severidad, servidor, backend, fuente, acknowledgement y downtime. Los encabezados permiten ordenar y la tabla pagina localmente los resultados.

El dashboard consulta APIs del mismo origen mediante polling HTTP. El intervalo inicial es 5 segundos y se puede cambiar a 10, 15, 30 o 60 segundos desde la barra superior; esa preferencia se conserva en el navegador. El dashboard recorre las páginas de alertas del backend en lotes de hasta 500, sin truncar la lista por el límite de una página. Los filtros, el orden y la página actual se mantienen durante cada actualización. El selector de tema claro/oscuro también se guarda localmente.

La pestaña **Historial de cambios** muestra aperturas, cambios de estado/atributos y recuperaciones que el backend observó en snapshots. No puede mostrar cambios transitorios que ocurrieron entre dos recepciones. La sección **Fuentes de información** muestra la antigüedad de cada webhook y el estado de cada servidor Thruk por separado. Los mensajes manuales `type: test` no crean fuentes.

## Ejecutar

Desde `web/`, copia `.env.example` a `.env`. El valor predeterminado publica solo en loopback, para uso en el mismo equipo:

```powershell
Copy-Item .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f
```

El backend queda en `http://127.0.0.1:8080`. El health check está en `/healthz`. Para parar sin borrar datos:

```powershell
docker compose down
```

En Linux, desde `web/`:

```sh
cp .env.example .env
docker compose up -d --build
docker compose ps
docker compose logs -f
docker compose down
```

SQLite persiste en el volumen nombrado `naftamon_web_data`, incluso al recrear el contenedor. `docker compose down -v` elimina ese volumen y borra la base de datos; úsalo solo si quieres reiniciar todos los datos.

## Recibir desde NaftaMon Desktop en otra computadora

En `.env`, configura `BIND_ADDRESS` con la IP LAN privada del equipo host y `WEB_PORT` con el puerto permitido, por ejemplo:

```dotenv
BIND_ADDRESS=192.168.1.200
WEB_PORT=8080
```

Recrea el servicio con `docker compose up -d`. En la configuración del Gateway de Desktop usa:

```text
http://192.168.1.200:8080/api/v1/ingest
```

Reemplaza la dirección por la IP real del host. No uses `localhost` desde la computadora Desktop: allí apuntaría a la propia computadora Desktop.

Como no hay autenticación, limita el acceso en Windows Defender Firewall a la interfaz y subred confiables. Por ejemplo, desde PowerShell elevado, adapta IP, puerto y subred antes de crear la regla:

```powershell
New-NetFirewallRule -DisplayName "NaftaMon Web LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalAddress 192.168.1.200 -LocalPort 8080 -RemoteAddress 192.168.1.0/24 -Profile Private
```

Usa una VLAN/subred de confianza; no habilites reglas para `Any`, perfil `Public`, port forwarding ni acceso entrante desde Internet. Revisa también las reglas de Docker Desktop y del router. HTTP no cifra el tráfico. Cualquier equipo que alcance el puerto puede enviar datos falsos y leer las APIs.

En Docker Engine con backend iptables, Docker enruta los puertos publicados antes de las reglas habituales de UFW. No confíes solamente en `ufw allow/deny` para restringir este puerto; usa el firewall de red o reglas verificadas en `DOCKER-USER`. Sustituye interfaz y subred por los valores reales:

```sh
sudo iptables -I DOCKER-USER 1 -i enp3s0 -p tcp -s 192.168.1.0/24 -m conntrack --ctorigdstport 8080 -j ACCEPT
sudo iptables -I DOCKER-USER 2 -i enp3s0 -p tcp -m conntrack --ctorigdstport 8080 -j DROP
```

Estas reglas aplican al backend iptables de Docker; no son persistentes en todas las distribuciones. Configura su persistencia con el gestor de firewall del host y prueba desde una IP permitida y otra denegada. Si Docker usa backend nftables, configura la regla equivalente con nftables. Consulta la [documentación de Docker sobre firewall y puertos publicados](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

## Configuración

| Variable | Predeterminado | Función |
| --- | --- | --- |
| `WEB_PORT` | `8080` | Puerto del host publicado por Compose. |
| `BIND_ADDRESS` | `127.0.0.1` | Dirección del host donde Docker publica el puerto. Elige una IP LAN privada para recibir conexiones remotas. |
| `OFFLINE_AFTER_SECONDS` | `900` | Segundos desde la última recepción antes de marcar una fuente offline. Configúralo por encima del intervalo del Gateway; ese intervalo no viaja en el JSON. |
| `MAX_REQUEST_BYTES` | `8388608` | Tamaño máximo del POST (8 MiB). |
| `HISTORY_RETENTION_DAYS` | `90` | Retención del historial de cambios. `0` desactiva la limpieza automática. |
| `NOTIFICATION_ALLOWED_DESTINATION_CIDRS` | vacío | Rangos privados permitidos para destinos Gotify, ntfy o webhook; usa un CIDR LAN concreto, por ejemplo `192.168.1.0/24`. |
| `NOTIFICATION_KEY_DIR` | `./.local` | Carpeta del host con `notification.key`; puede apuntar a otra carpeta local para pruebas aisladas. |

Compose carga estos valores desde `.env` o variables de entorno. `.env` no contiene credenciales. La clave de cifrado de tokens de notificación se monta desde `web/.local/notification.key` en modo lectura y se mantiene fuera del volumen SQLite, del contexto de construcción y de Git.

## Endpoint de ingesta

El Gateway actual envía `Content-Type: application/json; charset=utf-8` y `User-Agent: NaftaMon WebhookGateway`; el backend acepta el parámetro `charset` y no exige ese User-Agent ni ningún header de identidad. Valida `source` como el literal actual `NaftaMon`. La ruta no requiere API key, bearer token ni login.

`POST /api/v1/ingest` recibe el JSON real descrito en [docs/webhook-contract.md](docs/webhook-contract.md). No exige headers especiales, API key, bearer token ni login. Los mensajes `type: test` se aceptan con `202`, pero no crean una fuente, refrescan su conectividad ni alteran snapshot, alertas o historial.

Respuestas: `202` para mensajes aceptados; `400` para JSON malformado; `413` cuando el cuerpo supera el límite; `415` si no es `application/json`; `422` para un JSON válido con estructura o semántica incorrecta; `503` si SQLite no puede guardar el snapshot.

Los snapshots completos y parciales se procesan por servidor. Solo `status: ok` confirma las alertas de ese servidor; el fallo/pending conserva las alertas conocidas como no confirmadas. Una lista vacía de alertas en servidor `ok` resuelve las alertas anteriores de ese servidor. La fuente offline conserva el último estado conocido y presenta sus alertas como stale.

El formato no tiene ID de instalación. Para evitar una identidad inventada, el backend agrupa provisionalmente por `source` y la IP observada del peer TCP. Es una aproximación no estable: dos instancias en la misma computadora se mezclan; NAT, contenedores y proxies pueden alterar la agrupación. `source` y la IP no son credenciales. Una extensión opcional `instance_id` en Desktop sería la solución compatible futura.

En esta versión debe haber como máximo un Gateway lógico detrás de cada IP que el receptor observa, con nombres de servidores Thruk que no colisionen. Dos Desktop detrás de la misma IP/NAT no se distinguen y sus snapshots podrían reemplazarse o resolver alertas entre sí. El JSON actual no permite corregirlo desde NaftaMon Web sin cambiar el contrato.

## APIs sin autenticación

Todas las rutas son mismo origen, no requieren autenticación y devuelven JSON:

| Método y ruta | Uso |
| --- | --- |
| `GET /` | Dashboard visual, sin autenticación. |
| `GET /api/v1/alerts` | Alertas activas con `source_id`, `server`, `state`, `type`, `stale`, `limit` y `offset` opcionales. |
| `GET /api/v1/stats` | Total de alertas, agrupación por estado y fuentes online/offline. |
| `GET /api/v1/sources` | Última recepción, frescura y estado de consultas por servidor. |
| `GET /api/v1/history` | Transiciones observadas; filtros `source_id`, `server`, `limit`, `offset`. |
| `GET /healthz` | Salud del proceso y comprobación SQLite. |

La recuperación se fecha cuando el servidor web observa que la alerta ya no está en un snapshot autoritativo `ok`; el Gateway no transmite el instante exacto de recuperación. Cambios transitorios entre snapshots no se pueden reconstruir.

## Notificaciones opcionales

El motor de notificaciones está apagado inicialmente. Los canales y reglas nuevos también comienzan deshabilitados. El contenedor `notifications` procesa la cola SQLite en segundo plano; si un proveedor falla, el receptor sigue almacenando snapshots. La sección **Notifications** permite configurar Rules, Channels, History y Settings desde el dashboard. La prueba de una regla solo muestra coincidencias; el botón de prueba de un canal sí intenta enviar un mensaje al destino configurado.

Los eventos admitidos son apertura, cambio de estado, recuperación observada y recordatorio opcional. Un snapshot idéntico no vuelve a notificar. Una recuperación exige una respuesta `ok` del servidor Thruk correspondiente; errores y silencios no se tratan como recuperación. Las condiciones solo pueden usar los campos realmente presentes en el contrato. No hay plugin output ni un ID estable de instalación, por lo que no se pueden filtrar por esos datos. Las plantillas tampoco admiten `{output}`. Consulta el [diseño y límites del motor](docs/notification-design.md).

Los destinos soportados son Telegram, Gotify, ntfy y webhook HTTP. Telegram requiere bot token y chat ID; Gotify requiere URL de servidor y application token; ntfy requiere URL y topic, con credenciales opcionales; webhook HTTP requiere URL y admite headers y cuerpo JSON controlados. Las credenciales se cifran con una clave local externa a SQLite. Para crearla desde `web/`, primero construye la imagen y crea el directorio local:

```powershell
docker compose build
New-Item -ItemType Directory -Force .local | Out-Null
docker compose run --rm --no-deps web python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" | Set-Content -NoNewline .local/notification.key
docker compose up -d
```

En Linux:

```sh
umask 077
docker compose build
mkdir -p .local
docker compose run --rm --no-deps web python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' > .local/notification.key
chmod 600 .local/notification.key
docker compose up -d
```

Guarda una copia de esa clave **separada** del backup SQLite, con acceso restringido. Si se pierde, los tokens cifrados existentes no pueden recuperarse; hay que configurarlos otra vez. Si cambias la clave sin recifrar los tokens, los canales con credenciales dejarán de enviar. `.local/` y `.env` no se incluyen en Git ni en la imagen. Para un Gotify o ntfy en LAN, declara solo su subred privada necesaria en `NOTIFICATION_ALLOWED_DESTINATION_CIDRS`, por ejemplo `192.168.1.0/24`, y recrea `web` y `notifications`. No uses un rango amplio como `0.0.0.0/0`.

Puedes inspeccionar el worker con `docker compose logs --tail 100 notifications` y su estado con `docker compose ps`. El historial visual y la API de notificaciones muestran intentos, resultado y errores saneados. Los reintentos son limitados; debido a la ventana entre un POST externo exitoso y su confirmación en SQLite, un proveedor sin idempotencia puede recibir un duplicado tras un corte abrupto.

## Pruebas y operación

Las pruebas usan fixtures inventados y una SQLite temporal, nunca servicios productivos:

```powershell
docker compose --profile test run --build --rm test
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 web
Invoke-RestMethod http://127.0.0.1:8080/healthz
Invoke-RestMethod http://127.0.0.1:8080/api/v1/sources
docker compose restart web
```

La suite incluye condiciones de reglas, eventos, reintentos y proveedores con transportes HTTP simulados. Para validar también el recorrido HTTP real `ingest → SQLite → worker → mock` en un proyecto separado, desde PowerShell en `web/`:

```powershell
Copy-Item .env.notifications-test.example .env.notifications-test
New-Item -ItemType Directory -Force .test-local | Out-Null
docker compose --env-file .env.notifications-test -p naftamon-notif-lab --profile mock up -d --build
docker compose --env-file .env.notifications-test -p naftamon-notif-lab exec -T web python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" | Set-Content -NoNewline .test-local/notification.key
docker compose --env-file .env.notifications-test -p naftamon-notif-lab --profile mock --profile test run --build --rm test python tests/integration_notification_smoke.py
docker compose --env-file .env.notifications-test -p naftamon-notif-lab --profile mock down -v
```

El script usa un puerto loopback separado, un volumen y una clave de prueba. Inspecciona el mock con `docker compose --env-file .env.notifications-test -p naftamon-notif-lab exec -T mock-provider python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/received').read().decode())"`. Usa solo tokens ficticios. El perfil `mock` no publica el receptor de prueba al host. Si el rango Docker local no está dentro de `172.16.0.0/12`, ajusta `NOTIFICATION_ALLOWED_DESTINATION_CIDRS` al CIDR exacto antes de levantar los servicios. No ejecutes `down -v` sobre el proyecto normal que almacena tus datos.

En Linux, los comandos equivalentes son:

```sh
docker compose --profile test run --build --rm test
docker compose up -d --build
docker compose ps
docker compose logs --tail 100 web
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/api/v1/sources
docker compose restart web
```

### Backup y restauración SQLite

Usa la API de backup de SQLite para incluir de forma consistente las escrituras en WAL. Guarda la copia fuera del repositorio y del volumen, y protégela como dato de monitoreo. Ejemplo PowerShell:

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\naftamon-backups" | Out-Null
docker compose exec -T web python -c "import sqlite3; s=sqlite3.connect('/data/naftamon.sqlite3'); d=sqlite3.connect('/tmp/naftamon-backup.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose cp web:/tmp/naftamon-backup.sqlite3 "$env:USERPROFILE\naftamon-backups\naftamon-web-backup.sqlite3"
```

Para restaurar, detén el servicio, copia la base al volumen y usa la API SQLite para reemplazar el contenido:

```powershell
docker compose stop web notifications
docker compose cp "$env:USERPROFILE\naftamon-backups\naftamon-web-backup.sqlite3" web:/data/naftamon-restore.sqlite3
docker compose run --rm --no-deps web python -c "import os,sqlite3; s=sqlite3.connect('/data/naftamon-restore.sqlite3'); d=sqlite3.connect('/data/naftamon.sqlite3'); s.backup(d); d.close(); s.close(); os.remove('/data/naftamon-restore.sqlite3')"
docker compose start web notifications
Invoke-RestMethod http://127.0.0.1:8080/healthz
```

En Linux se usa `$HOME/naftamon-backups` fuera del repositorio. El directorio y el archivo quedan accesibles solo al usuario actual:

```sh
umask 077
mkdir -p "$HOME/naftamon-backups"
chmod 700 "$HOME/naftamon-backups"
docker compose exec -T web python -c 'import sqlite3; s=sqlite3.connect("/data/naftamon.sqlite3"); d=sqlite3.connect("/tmp/naftamon-backup.sqlite3"); s.backup(d); d.close(); s.close()'
docker compose cp web:/tmp/naftamon-backup.sqlite3 "$HOME/naftamon-backups/naftamon-web-backup.sqlite3"
chmod 600 "$HOME/naftamon-backups/naftamon-web-backup.sqlite3"
docker compose stop web notifications
docker compose cp "$HOME/naftamon-backups/naftamon-web-backup.sqlite3" web:/data/naftamon-restore.sqlite3
docker compose run --rm --no-deps web python -c 'import os,sqlite3; s=sqlite3.connect("/data/naftamon-restore.sqlite3"); d=sqlite3.connect("/data/naftamon.sqlite3"); s.backup(d); d.close(); s.close(); os.remove("/data/naftamon-restore.sqlite3")'
docker compose start web notifications
curl --fail http://127.0.0.1:8080/healthz
```

Haz una copia antes de actualizar. `docker compose down` conserva el volumen; `docker compose down -v` lo elimina y borra la base, así que no es un procedimiento de backup.

### Actualización

Descarga o cambia al código de la versión que vas a desplegar, haz una copia de SQLite y ejecuta `docker compose up -d --build` desde `web/`. Las migraciones del esquema se aplican al iniciar. Comprueba `docker compose ps`, `/healthz` y los logs antes de volver a configurar el Gateway.

### Diagnóstico

- `docker compose ps` muestra el estado del healthcheck. `healthy` verifica que el esquema versionado y las tablas requeridas respondan a consultas pequeñas; no ejecuta un recorrido completo de integridad ni comprueba que el volumen permita escrituras. `unhealthy` devuelve `503` en `/healthz`.
- `docker compose logs --tail 100 web` muestra inicio y códigos HTTP, pero no guarda cuerpos de webhook.
- `docker compose restart web` reinicia el proceso y conserva el volumen. `restart: unless-stopped` reinicia el contenedor si el proceso termina; Docker Compose no reinicia automáticamente un contenedor solo porque el healthcheck quede `unhealthy`.
- `connection refused`: confirma `WEB_PORT`, `BIND_ADDRESS`, `docker compose ps` y la regla de firewall.
- `400`, `413`, `415` o `422` en POST: revisa UTF-8 JSON, Content-Type, límite de 8 MiB y esquema de [webhook-contract.md](docs/webhook-contract.md).
- Sin fuentes: confirma que el Gateway use la IP del host Docker, no `localhost`, y que la regla de firewall permita la computadora emisora.

## Seguridad y límites operativos

El requisito de esta versión es no tener autenticación: cualquier cliente que alcance el puerto puede leer alertas, enviar snapshots falsos, modificar reglas y destinos, y solicitar envíos de prueba. Úsala solo en una LAN confiable o VPN, no la publiques en Internet y limita el acceso desde el firewall del host y la segmentación de red. CORS no autentica clientes ni sustituye estas restricciones. El Gateway no incluye credenciales de Thruk en su JSON; el backend no registra cuerpos y nunca ejecuta los valores recibidos. SQL usa parámetros y el frontend inserta datos como texto. Los destinos de notificación pueden transportar datos de infraestructura fuera de la LAN si el operador los configura; revísalos antes de habilitarlos.

La identidad del emisor sigue siendo provisional (`source` + IP observada); conexiones compartidas por NAT o proxy se mezclan. La validación física entre dos computadoras y el despliegue en Docker Engine Linux dependen de la red y firewall del host; en esta entrega se verificaron Docker Desktop en Windows 11 y dos emisores dentro de contenedores aislados.

Para inspeccionar una recepción, usa el dashboard o consulta `/api/v1/alerts`, `/api/v1/sources` y `/api/v1/history`; los logs del contenedor muestran el estado HTTP, no registran el cuerpo del webhook. El reinicio del contenedor conserva el volumen `naftamon_web_data`.

La suite de pytest cubre persistencia al recrear la aplicación sobre el mismo archivo SQLite. Para probar restart, force-recreate, backup online y restauración real del volumen mediante Docker en Windows PowerShell, ejecuta desde `web/`:

```powershell
.\tests\test-container-persistence.ps1
```

El script crea un proyecto Compose y puerto loopback únicos, envía un snapshot ficticio, reinicia y recrea el servicio `web`, genera una copia con `sqlite3.Connection.backup`, restaura el fixture en el volumen y comprueba la alerta. Al terminar elimina únicamente el proyecto temporal, su volumen y el archivo de backup temporal.

Las dependencias de ejecución y prueba están fijadas en `requirements.txt` y `requirements-dev.txt`. Docker necesita acceso a PyPI durante la construcción inicial; una vez construida la imagen, backend y API no llaman a servicios externos.
