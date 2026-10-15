# Contrato del Webhook Gateway de NaftaMon Desktop

## Alcance y fuente

Contrato basado en la implementación actual de src/webhookgateway.cpp, src/webhookgateway.h, src/core.h, src/thruk.cpp y las verificaciones en tests/test_main.cpp. El receptor debe tratar el contenido como datos no confiables.

El Gateway realiza HTTP POST con Content-Type: application/json; charset=utf-8 y User-Agent: NaftaMon WebhookGateway. No envía API keys, bearer tokens ni credenciales de Thruk. La URL configurada es el destino HTTP y no forma parte del JSON.

## Mensaje periódico: snapshot actual

Cada envío periódico es un snapshot del estado que conoce NaftaMon; no es un evento individual y no incluye número de secuencia ni identificador de entrega. El Gateway reenvía snapshots aunque no hayan cambiado.

### Campos de raíz

| Campo | Tipo | Semántica |
| --- | --- | --- |
| timestamp | string ISO 8601 UTC con milisegundos | Momento en que el Gateway serializó el snapshot, no hora exacta del polling de Thruk. |
| source | string | Valor constante NaftaMon; no identifica una instalación. |
| complete | boolean | true solo si hay al menos un servidor y todos están en estado ok. |
| servers | array | Estado actual de cada servidor recibido por el Gateway. |
| alerts | array | Alertas activas (estados distintos de UP) de los servidores cuyo estado es ok en este envío. Incluyen `status_information`. |
| ok_statuses | array | Hosts UP y servicios OK de los mismos servidores consultados. Completa la representación de monitoreo sin convertir los estados saludables en alertas. |

Cada elemento de servers contiene server (string: nombre configurado, no URL ni ID inmutable) y status (string: ok, error o pending). pending significa que todavía no hay datos exitosos; error significa que la última consulta informó un error.

### Campos de cada alerta

| Campo | Tipo | Semántica |
| --- | --- | --- |
| server | string | Nombre configurado del servidor Thruk. |
| backend | string | peer_name de Thruk; puede estar vacío. |
| host | string | Nombre del host informado por Thruk. |
| service | string | Para host queda vacío. Para servicio puede ser la descripción o el display name configurado en NaftaMon. |
| type | string | host o service. Distingue alerta de host (service vacío) de alerta de servicio. |
| state | string | Estado normalizado: UNKNOWN, WARNING, CRITICAL, UNREACHABLE o DOWN. Los objetos UP no se envían como alertas. |
| status_information | string | Valor de `Item.output` de NaftaMon (plugin output de Thruk). Puede estar vacío. |
| hard | boolean | true cuando Thruk informa estado HARD (state_type == 1). |
| acknowledged | boolean | Estado de acknowledgement de Thruk. |
| downtime | boolean | true si el nivel de downtime programado es mayor que cero. |
| last_check | string ISO 8601 UTC | last_check de Thruk convertido de epoch en segundos. |
| last_change | string ISO 8601 UTC | last_state_change de Thruk convertido de epoch en segundos. |

Los snapshots incluyen el plugin output como `status_information`; no incluyen perfdata, intentos, URL de Thruk, usuario, contraseña ni token. No aplican filtros visuales, búsqueda, acknowledgement o downtime. Los backends excluidos en la configuración de NaftaMon no aparecerán en los datos consultados.

Cada elemento de `ok_statuses` usa los mismos campos que una alerta y conserva `server`, `backend`, `host`, `service`, `type`, `state` (`UP`) y `status_information`. Las alertas conservan su ubicación y significado actuales. La unión de `alerts` y `ok_statuses` representa los objetos consultados; no debe interpretarse `ok_statuses` como eventos de alerta o notificación. El array raíz `servers` sigue describiendo conectividad de cada servidor Thruk.

### Ejemplo: snapshot completo con una alerta

~~~json
{
  "timestamp": "2026-10-14T22:00:00.000Z",
  "source": "NaftaMon",
  "complete": true,
  "servers": [
    {"server": "Thruk oficina", "status": "ok"}
  ],
  "alerts": [
    {
      "server": "Thruk oficina",
      "backend": "alpha",
      "host": "db01",
      "service": "disk",
      "type": "service",
      "state": "CRITICAL",
      "status_information": "DISK CRITICAL — 94% used",
      "hard": true,
      "acknowledged": false,
      "downtime": false,
      "last_check": "2026-10-14T21:59:00Z",
      "last_change": "2026-10-14T21:30:00Z"
    }
  ]
}
~~~

### Estado saludable de host y servicio

Los objetos UP se publican en `ok_statuses`, separados de `alerts`. De este modo el receptor puede mostrar estados OK sin crear episodios ni notificaciones de alerta.

~~~json
{
  "timestamp": "2026-10-14T22:00:00.000Z",
  "source": "NaftaMon",
  "complete": true,
  "servers": [{"server": "Thruk oficina", "status": "ok"}],
  "alerts": [],
  "ok_statuses": [
    {
      "server": "Thruk oficina", "backend": "alpha", "host": "web01", "service": "",
      "type": "host", "state": "UP", "status_information": "PING OK",
      "hard": true, "acknowledged": false, "downtime": false,
      "last_check": "2026-10-14T21:59:00Z", "last_change": "2026-10-14T21:30:00Z"
    },
    {
      "server": "Thruk oficina", "backend": "alpha", "host": "web01", "service": "ssh",
      "type": "service", "state": "UP", "status_information": "SSH OK",
      "hard": true, "acknowledged": false, "downtime": false,
      "last_check": "2026-10-14T21:59:00Z", "last_change": "2026-10-14T21:30:00Z"
    }
  ]
}
~~~

### Ejemplo: consulta exitosa sin alertas

Una lista `alerts` vacía con complete true y servidores ok significa que el Gateway consultó satisfactoriamente y no encontró alertas activas. Puede haber objetos normales en `ok_statuses`.

~~~json
{
  "timestamp": "2026-10-14T22:01:00.000Z",
  "source": "NaftaMon",
  "complete": true,
  "servers": [
    {"server": "Thruk oficina", "status": "ok"}
  ],
  "alerts": [],
  "ok_statuses": []
}
~~~

### Ejemplo: snapshot parcial por error de consulta

Las alertas y estados saludables del servidor ok son actuales. El Gateway omite datos cacheados de servidores error o pending. El receptor conserva sus datos previos como no confirmados, no los resuelve.

~~~json
{
  "timestamp": "2026-10-14T22:02:00.000Z",
  "source": "NaftaMon",
  "complete": false,
  "servers": [
    {"server": "Thruk oficina", "status": "ok"},
    {"server": "Thruk datacenter", "status": "error"}
  ],
  "alerts": [
    {
      "server": "Thruk oficina",
      "backend": "alpha",
      "host": "web01",
      "service": "",
      "type": "host",
      "state": "DOWN",
      "status_information": "PING CRITICAL",
      "hard": true,
      "acknowledged": false,
      "downtime": false,
      "last_check": "2026-10-14T22:01:50Z",
      "last_change": "2026-10-14T21:45:00Z"
    }
  ],
  "ok_statuses": []
}
~~~

### Mensaje de prueba manual

Test Webhook envía un mensaje separado, sin complete, servers ni alerts. No debe reemplazar el snapshot ni reconciliar alertas.

~~~json
{
  "type": "test",
  "test": true,
  "source": "NaftaMon",
  "timestamp": "2026-10-14T22:03:00.000Z",
  "message": "NaftaMon test webhook"
}
~~~

## Semántica de recepción recomendada

- Identificar la prueba por type == test y test == true; aceptarla sin reconciliar alertas.
- En un snapshot normal, un servidor ok es autoritativo para ese servidor. La ausencia de una alerta en el array significa que no está activa en ese snapshot.
- Para error o pending, conservar alertas previas como stale/no confirmadas; no inferir recuperación.
- complete == false no invalida servidores individuales ok: se pueden reconciliar por servidor. Indica que al menos una parte está incompleta.
- `alerts` sigue siendo el único conjunto de alertas. `ok_statuses` describe hosts y servicios UP/OK y no debe activar reglas o notificaciones de alerta.
- El backend guarda `status_information` en alertas y estados UP. Si un elemento entrante incluye una cadena vacía, actualiza el valor almacenado a vacío; si un elemento de una versión antigua omite la propiedad, conserva el valor previo. Cambiar solo `status_information` no crea una transición de alerta.
- `GET /api/v1/alerts` incluye `status_information` en cada alerta. `GET /api/v1/statuses` combina alertas activas con los objetos UP más recientes e incluye la misma información y un indicador `stale`. La interfaz visual actual todavía no muestra el campo ni consume los objetos UP.
- Una snapshot con timestamp anterior al último aceptado de esa fuente recibe una respuesta de aceptación sin modificar el snapshot ni los estados persistidos. Su hora de recepción se conserva para reflejar que hubo tráfico.
- complete true, servidores ok y alerts vacío es distinto de una fuente silenciosa o de un servidor inaccesible.
- Una recuperación solo se observa cuando una alerta deja de aparecer en un snapshot ok. No se incluye evento ni timestamp explícito de recuperación.

## Identidad y limitaciones

El JSON no contiene ID estable de la instalación emisora. source siempre vale NaftaMon; server es un nombre editable; backend identifica el peer de Thruk, no al emisor. Dos instalaciones con nombres de servidor iguales no se distinguen de forma confiable solo con el cuerpo JSON.

El endpoint de la etapa 2 es exactamente `POST /api/v1/ingest` y no añade headers ni credenciales obligatorios. Para agrupar datos sin cambiar el Gateway se usa provisionalmente el par `source` + IP observada de la conexión TCP. Esto no identifica instalaciones de forma confiable: procesos en una computadora comparten IP, dispositivos detrás de NAT pueden compartir dirección, y un proxy puede ocultar el emisor real. El receptor ignora cabeceras de forwarding del cliente y no considera la IP autenticación. La solución de largo plazo es un `instance_id` opcional estable en el JSON, conservando aceptación de clientes actuales.

Futura mejora compatible: añadir instance_id opcional o una cabecera de identificación estable, manteniendo aceptación de los mensajes actuales. No se modifica Gateway en esta etapa.

Otros límites:

- No hay ID de alerta, secuencia, versión de schema ni ID de entrega. Clave compuesta sugerida: grupo provisional source + peer IP + server + backend + type + host + service.
- `status_information` contiene literalmente el `plugin_output` recibido de Thruk, incluso saltos de línea, espacios, caracteres especiales y Unicode. La interfaz Desktop usa una vista normalizada por separado para conservar su presentación y filtros existentes.
- Un receptor estricto antiguo puede rechazar los campos nuevos. Si recibe HTTP 422 con un cuerpo que identifica `status_information` u `ok_statuses` como campo no admitido, Desktop reintenta una vez con el formato anterior y usa ese formato hasta que se guarden de nuevo las opciones del Gateway o cambie su configuración. Un HTTP 422 por otra causa no activa el fallback. Guardar las opciones permite comprobar si el receptor ya fue actualizado.
- No se informa el intervalo configurado ni cuándo terminó el último polling; el receptor debe guardar su received_at.
- last_change informa el cambio conocido por Thruk, no la hora exacta de recuperación.
- No hay reintentos durables prometidos si el receptor está caído; los snapshots no recibidos no se reconstruyen.
- Cambios transitorios entre snapshots pueden no observarse.
- Si se usa display name para servicios, service puede cambiar respecto de la descripción real y afectar la clave compuesta.
