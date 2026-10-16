# Contrato del Webhook de NaftaMon

## Versiones

El receptor acepta snapshots históricos v1 y snapshots v2. Desktop nuevo envía v2; cada envío es un snapshot completo de las alertas activas que pudo consultar, no un evento individual.

### Raíz del snapshot v2

| Campo | Tipo | Significado |
| --- | --- | --- |
| `schema_version` | `2` | Versión del contrato del snapshot. |
| `source` | string | Valor constante `NaftaMon`; nombre de producto, no identificador de instancia. |
| `source_id` | UUID string | Identificador persistente de esta instalación Desktop, guardado en QSettings. Permite distinguir emisores que comparten IP. |
| `snapshot_id` | UUID string | Identificador único del snapshot. Reintentos del mismo envío reutilizan el mismo ID y el mismo cuerpo. |
| `timestamp` | ISO 8601 UTC | Hora de creación del snapshot. No es la hora exacta de cada consulta a Thruk. |
| `complete` | boolean | `true` si hay al menos un servidor y todos respondieron `ok`. |
| `servers` | array | Estado de cada servidor configurado: `ok`, `error` o `pending`. |
| `alerts` | array | Problemas RAW (estados distintos de UP) de servidores `ok`. Es la única sección que reconcilia alertas y notificaciones. |
| `summary` | object | Contadores `raw` y `visible` del mismo snapshot; cada uno contiene `total` y `by_state`. |

V2 no contiene `ok_statuses[]`: los hasta cientos de miles de hosts y servicios UP no se transmiten. No se añaden consultas a Thruk. Los problemas conservan `status_information` y sus demás atributos.

### Campos de una alerta

| Campo | Tipo | Significado |
| --- | --- | --- |
| `server` | string | Nombre configurado del servidor Thruk. |
| `backend` | string | Backend `peer_name` de Thruk; puede estar vacío. |
| `host` | string | Host informado por Thruk. |
| `service` | string | Descripción o display name del servicio; vacío para alertas de host. |
| `type` | `host` o `service` | Tipo de objeto. |
| `state` | string | `CRITICAL`, `WARNING`, `UNKNOWN`, `DOWN` o `UNREACHABLE`. |
| `status_information` | string | `Item.output` tal como llega desde Thruk, incluidos Unicode, espacios y saltos de línea; puede ser `""`. |
| `hard` | boolean | Estado HARD de Thruk. |
| `acknowledged` | boolean | ACK actual. |
| `downtime` | boolean | Indica downtime programado. |
| `last_check`, `last_change` | ISO 8601 UTC | Tiempos del objeto informados por Thruk. |
| `visible_in_desktop` | boolean | Resultado de los filtros de alertas compartidos de Desktop para esta fila. |

`visible_in_desktop` se calcula llamando a `applyFilters()` sobre el snapshot RAW. Incluye ACK/downtime/flapping, estados soft y filtros configurados. El filtro rápido `New` se aplica después. No incorpora búsqueda temporal, el chip de severidad ni la página actual. `alerts[]` conserva tanto las filas visibles como las filtradas: cambiar su visibilidad no abre ni recupera alertas.

### Ejemplo v2

~~~json
{
  "schema_version": 2,
  "source": "NaftaMon",
  "source_id": "96ce1ec3-6268-4c03-bdb7-d77caa77bb88",
  "snapshot_id": "c9e37440-1d36-4a75-8d9e-7bf8c76c060b",
  "timestamp": "2026-10-16T19:00:00.000Z",
  "complete": true,
  "servers": [{"server": "Thruk oficina", "status": "ok"}],
  "alerts": [{
    "server": "Thruk oficina", "backend": "alpha", "host": "db01", "service": "disk",
    "type": "service", "state": "CRITICAL", "status_information": "DISK CRITICAL — 94% ✓",
    "hard": true, "acknowledged": false, "downtime": false,
    "last_check": "2026-10-16T18:59:00Z", "last_change": "2026-10-16T18:30:00Z",
    "visible_in_desktop": true
  }],
  "summary": {
    "raw": {"total": 1, "by_state": {"CRITICAL": 1}},
    "visible": {"total": 1, "by_state": {"CRITICAL": 1}}
  }
}
~~~

Una lista vacía `alerts: []` en un servidor `ok` significa que la consulta fue correcta y que no quedan problemas activos en ese servidor. Una lista vacía de un servidor `error` o `pending` **no** confirma recuperación: el receptor conserva el estado previo. `complete:false` permite reconciliar individualmente los servidores `ok`; preserva los que fallaron.

## Reconciliación en NaftaMon Web

- La clave persistida del problema es `source_id + server + backend + type + host + service`.
- En un servidor `ok`, `alerts[]` es autoritativo. Si un problema RAW desaparece en un snapshot aceptado, se genera su recuperación. Error, pendiente, snapshot antiguo y reintento duplicado no generan recuperaciones.
- `visible_in_desktop` actualiza solo la columna de presentación; no altera historial, episodios, eventos ni reglas de notificación.
- Los contadores globales RAW de `/api/v1/stats` se mantienen en `alerts`. `desktop_visible_alerts` es el conjunto visible por filtros de Desktop. La API `/api/v1/statuses` conserva su vista histórica completa por defecto (`view=all`); el dashboard inicia en `view=desktop` y permite elegir `view=all`.
- Al recibir v2 por primera vez para una fuente, las filas UP heredadas de v1 se eliminan de `monitored_statuses`. No son alertas y su retiro no produce recuperaciones ni notificaciones. Se conservan reglas e historial de notificaciones.
- La recepción de un `snapshot_id` ya procesado con el mismo timestamp y contenido devuelve `202` como duplicado sin avanzar la revisión ni repetir eventos. Reutilizar el ID con distinto contenido devuelve `409`.

Los receptores v1 continúan aceptados: `schema_version`, `source_id`, `snapshot_id`, `summary` y `visible_in_desktop` pueden faltar. Una alerta v1 sin `visible_in_desktop` queda visible para mantener el comportamiento anterior. V1 puede incluir `ok_statuses[]`; se acepta para instalaciones Web que aún almacenan estados UP. Desktop solo usa el formato de compatibilidad v1 cuando el servidor rechaza v2 por campos extendidos; en ese modo no envía las extensiones v2.

## HTTP, timeout y entrega

Desktop envía `POST` con `Content-Type: application/json; charset=utf-8`, `User-Agent: NaftaMon WebhookGateway` y `Content-Encoding: gzip`. Web acepta gzip y JSON sin comprimir y limita por separado bytes de red y bytes descomprimidos. Si un receptor antiguo rechaza gzip con `400`, `415` o `501`, Desktop reintenta el mismo cuerpo v2 sin compresión.

El timeout del Gateway se configura entre 5 y 300 segundos (30 por defecto). Hay como máximo una solicitud periódica activa. Se realizan hasta tres intentos para timeout, pérdida de conexión, `429` y `5xx`; se conserva el mismo `snapshot_id` y cuerpo para que Web pueda deduplicar. La cancelación por cambio de configuración o desactivación se identifica por separado. Los logs del Gateway registran cantidad de alertas, bytes, duración, código HTTP y categoría de error; no incluyen `status_information`.

## Prueba manual

El botón Test Webhook envía un mensaje separado con `type: "test"` y `test: true`. No contiene alertas ni modifica una fuente, snapshot, alertas o historial.

## Límites conocidos

- `server` es un nombre editable; `backend` identifica el backend Thruk y no la instalación Desktop. La clave de fuente v2 resuelve la identidad del emisor, pero renombrar servidores puede crear una identidad de servidor nueva.
- El cuerpo de un snapshot v2 crece con la cantidad de problemas y la longitud de `status_information`; sigue sujeto a los límites de request configurados en Web.
- No existe una secuencia de polling ni una cola durable sin límite. Los snapshots periódicos posteriores vuelven a reportar el estado actual.
- `status_information` es texto recibido de Thruk. El receptor y el dashboard deben tratarlo como dato, no como HTML ejecutable.
- El webhook no autentica al emisor. Usar solo LAN confiable o VPN y restringir el puerto con firewall.
