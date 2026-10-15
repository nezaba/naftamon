# NaftaMon Web — dashboard y backend

Dashboard local para ver alertas activas recibidas desde NaftaMon Desktop. Incluye una interfaz responsive en español, APIs de lectura y recepción de snapshots con persistencia SQLite.

## Requisitos

- Docker Desktop con Compose v2.
- Para recibir datos desde otra computadora: ambos equipos en una LAN de confianza y la IP LAN privada del equipo que corre Docker.

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
docker compose logs -f web
```

El backend queda en `http://127.0.0.1:8080`. El health check está en `/healthz`. Para parar sin borrar datos:

```powershell
docker compose down
```

SQLite persiste en el volumen nombrado `naftamon_web_data`, incluso al recrear el contenedor. `docker compose down -v` elimina ese volumen y borra la base de datos; úsalo solo si quieres reiniciar todos los datos.

## Recibir desde NaftaMon Desktop en otra computadora

En `.env`, configura `BIND_ADDRESS` con la IP LAN privada del equipo host y `WEB_PORT` con el puerto permitido, por ejemplo:

```dotenv
BIND_ADDRESS=192.168.1.20
WEB_PORT=8080
```

Recrea el servicio con `docker compose up -d`. En la configuración del Gateway de Desktop usa:

```text
http://192.168.1.20:8080/api/v1/ingest
```

Reemplaza la dirección por la IP real del host. No uses `localhost` desde la computadora Desktop: allí apuntaría a la propia computadora Desktop.

Como no hay autenticación, limita el acceso en Windows Defender Firewall a la interfaz y subred confiables. Por ejemplo, desde PowerShell elevado, adapta IP, puerto y subred antes de crear la regla:

```powershell
New-NetFirewallRule -DisplayName "NaftaMon Web LAN" -Direction Inbound -Action Allow -Protocol TCP -LocalAddress 192.168.1.20 -LocalPort 8080 -RemoteAddress 192.168.1.0/24 -Profile Private
```

Usa una VLAN/subred de confianza; no habilites reglas para `Any`, perfil `Public`, port forwarding ni acceso entrante desde Internet. Revisa también las reglas de Docker Desktop y del router. HTTP no cifra el tráfico. Cualquier equipo que alcance el puerto puede enviar datos falsos y leer las APIs.

## Configuración

| Variable | Predeterminado | Función |
| --- | --- | --- |
| `WEB_PORT` | `8080` | Puerto del host publicado por Compose. |
| `BIND_ADDRESS` | `127.0.0.1` | Dirección del host donde Docker publica el puerto. Elige una IP LAN privada para recibir conexiones remotas. |
| `OFFLINE_AFTER_SECONDS` | `900` | Segundos desde la última recepción antes de marcar una fuente offline. Configúralo por encima del intervalo del Gateway; ese intervalo no viaja en el JSON. |
| `MAX_REQUEST_BYTES` | `8388608` | Tamaño máximo del POST (8 MiB). |
| `HISTORY_RETENTION_DAYS` | `90` | Retención del historial de cambios. `0` desactiva la limpieza automática. |

Compose carga estos valores desde `.env` o variables de entorno. `.env` no contiene credenciales.

## Endpoint de ingesta

`POST /api/v1/ingest` recibe el JSON real descrito en [docs/webhook-contract.md](docs/webhook-contract.md). No exige headers especiales, API key, bearer token ni login. Los mensajes `type: test` se aceptan con `202`, pero no crean una fuente, refrescan su conectividad ni alteran snapshot, alertas o historial.

Respuestas: `202` para mensajes aceptados; `400` para JSON malformado; `413` cuando el cuerpo supera el límite; `415` si no es `application/json`; `422` para un JSON válido con estructura o semántica incorrecta; `503` si SQLite no puede guardar el snapshot.

Los snapshots completos y parciales se procesan por servidor. Solo `status: ok` confirma las alertas de ese servidor; el fallo/pending conserva las alertas conocidas como no confirmadas. Una lista vacía de alertas en servidor `ok` resuelve las alertas anteriores de ese servidor. La fuente offline conserva el último estado conocido y presenta sus alertas como stale.

El formato no tiene ID de instalación. Para evitar una identidad inventada, el backend agrupa provisionalmente por `source` y la IP observada del peer TCP. Es una aproximación no estable: dos instancias en la misma computadora se mezclan; NAT, contenedores y proxies pueden alterar la agrupación. `source` y la IP no son credenciales. Una extensión opcional `instance_id` en Desktop sería la solución compatible futura.

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

Para inspeccionar una recepción, usa el dashboard o consulta `/api/v1/alerts`, `/api/v1/sources` y `/api/v1/history`; los logs del contenedor muestran el estado HTTP, no registran el cuerpo del webhook. El reinicio del contenedor conserva el volumen `naftamon_web_data`.

La suite de pytest cubre persistencia al recrear la aplicación sobre el mismo archivo SQLite. Para probar además un reinicio real del contenedor en Windows PowerShell, ejecuta desde `web/`:

```powershell
.\tests\test-container-persistence.ps1
```

El script crea un proyecto Compose y puerto loopback únicos, envía un snapshot ficticio, reinicia y recrea el servicio `web`, comprueba que la alerta persiste en ambos casos y elimina únicamente el proyecto temporal y su volumen.

Las dependencias de ejecución y prueba están fijadas en `requirements.txt` y `requirements-dev.txt`. Docker necesita acceso a PyPI durante la construcción inicial; una vez construida la imagen, backend y API no llaman a servicios externos.
