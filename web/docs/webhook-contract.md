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
| alerts | array | Alertas activas de los servidores cuyo estado es ok en este envío. |

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
| hard | boolean | true cuando Thruk informa estado HARD (state_type == 1). |
| acknowledged | boolean | Estado de acknowledgement de Thruk. |
| downtime | boolean | true si el nivel de downtime programado es mayor que cero. |
| last_check | string ISO 8601 UTC | last_check de Thruk convertido de epoch en segundos. |
| last_change | string ISO 8601 UTC | last_state_change de Thruk convertido de epoch en segundos. |

Los snapshots no incluyen plugin output, perfdata, intentos, URL de Thruk, usuario, contraseña ni token. No aplican filtros visuales, búsqueda, acknowledgement o downtime. Los backends excluidos en la configuración de NaftaMon no aparecerán en los datos consultados.

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
      "hard": true,
      "acknowledged": false,
      "downtime": false,
      "last_check": "2026-10-14T21:59:00Z",
      "last_change": "2026-10-14T21:30:00Z"
    }
  ]
}
~~~

### Ejemplo: consulta exitosa sin alertas

Una lista vacía con complete true y servidores ok significa que el Gateway consultó satisfactoriamente y no encontró alertas activas en ese snapshot.

~~~json
{
  "timestamp": "2026-10-14T22:01:00.000Z",
  "source": "NaftaMon",
  "complete": true,
  "servers": [
    {"server": "Thruk oficina", "status": "ok"}
  ],
  "alerts": []
}
~~~

### Ejemplo: snapshot parcial por error de consulta

Las alertas del servidor ok son actuales. El Gateway omite alertas cacheadas de servidores error o pending. El receptor conserva sus últimas alertas conocidas como no confirmadas, no las resuelve.

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
      "hard": true,
      "acknowledged": false,
      "downtime": false,
      "last_check": "2026-10-14T22:01:50Z",
      "last_change": "2026-10-14T21:45:00Z"
    }
  ]
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
- complete true, servidores ok y alerts vacío es distinto de una fuente silenciosa o de un servidor inaccesible.
- Una recuperación solo se observa cuando una alerta deja de aparecer en un snapshot ok. No se incluye evento ni timestamp explícito de recuperación.

## Identidad y limitaciones

El JSON no contiene ID estable de la instalación emisora. source siempre vale NaftaMon; server es un nombre editable; backend identifica el peer de Thruk, no al emisor. Dos instalaciones con nombres de servidor iguales no se distinguen de forma confiable solo con el cuerpo JSON.

Solución compatible sin modificar Desktop: configurar una URL de recepción diferente por instalación, por ejemplo /webhooks/naftamon/oficina y /webhooks/naftamon/lab, y usar esa etiqueta de ruta para agrupar los datos. La URL del Gateway es configurable. La etiqueta es dato de agrupación, **no** credencial ni autenticación: cualquier cliente de la LAN puede falsificarla.

Futura mejora compatible: añadir instance_id opcional o una cabecera de identificación estable, manteniendo aceptación de los mensajes actuales. No se modifica Gateway en esta etapa.

Otros límites:

- No hay ID de alerta, secuencia, versión de schema ni ID de entrega. Clave compuesta sugerida: etiqueta de emisor + server + backend + type + host + service.
- No se informa el intervalo configurado ni cuándo terminó el último polling; el receptor debe guardar su received_at.
- last_change informa el cambio conocido por Thruk, no la hora exacta de recuperación.
- No hay reintentos durables prometidos si el receptor está caído; los snapshots no recibidos no se reconstruyen.
- Cambios transitorios entre snapshots pueden no observarse.
- Si se usa display name para servicios, service puede cambiar respecto de la descripción real y afectar la clave compuesta.
