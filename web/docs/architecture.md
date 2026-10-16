# NaftaMon Web — arquitectura y estado de implementación

## Estado

La etapa 1 definió esta arquitectura. La etapa 2 implementó receptor, API, persistencia SQLite y Compose. La etapa 3 sirve un dashboard local desde FastAPI; la etapa 4 valida recepción, despliegue y persistencia. La etapa 5 incorpora reglas y notificaciones según [notification-design.md](notification-design.md). NaftaMon Desktop no se modificó. El contrato real del receptor está en [webhook-contract.md](webhook-contract.md).

## Arquitectura

La aplicación es monolítica FastAPI servida por un contenedor Docker Compose. El mismo proceso sirve la UI, las APIs JSON y el receptor HTTP. HTML, CSS y JavaScript nativos se mantienen bajo `app/static` y se sirven desde el mismo origen. SQLite vive en un volumen persistente. No hacen falta React, Redis, PostgreSQL, Kubernetes, CDN ni servicios externos.

El mismo proceso sirve dashboard, APIs de lectura y el receptor HTTP. CSS y JavaScript se incluyen localmente. No se publica SQLite como servicio de red. Para el tráfico esperado, usar Uvicorn con un worker, imagen Python pequeña, usuario no privilegiado y healthcheck HTTP.

## Estructura actual

~~~
web/
  AGENTS.md
  Dockerfile
  compose.yaml
  .dockerignore
  .env.example
  README.md
  requirements.txt
  requirements-dev.txt
  app/
    models.py
    main.py
    settings.py
    db.py
    ingest.py
    static/
      index.html
      dashboard.css
      dashboard.js
  tests/
    conftest.py
    test_backend.py
    test_dashboard.py
    test-container-persistence.ps1
  docs/
    architecture.md
    webhook-contract.md
~~~

Mantener módulos pequeños por responsabilidad y acceso SQLite sencillo. Incorporar ORM o migraciones externas solo si el crecimiento lo justifica.

## Ingesta y semántica

1. Desktop hace POST JSON a la URL configurada en NaftaMon.
2. FastAPI valida tamaño, JSON y estructura del tipo de mensaje. Toda entrada se considera no confiable.
3. El endpoint requerido es `POST /api/v1/ingest`, sin etiqueta ni credencial obligatoria. Como el contrato no tiene instance_id, esta etapa agrupa por el campo `source` más la IP observada del peer TCP. Es una heurística débil: NAT, proxies y varias instalaciones en una misma computadora pueden mezclar emisores. Para esta versión, desplegar como máximo un Gateway lógico por IP observada. No es autenticación.
4. Los mensajes de prueba se aceptan aparte; no actualizan snapshot ni alertas.
5. Los snapshots periódicos se guardan y reconcilian en una transacción SQLite que reserva el escritor antes de leer/incrementar la revisión; POST simultáneos quedan serializados.
6. Por cada servidor ok, su lista de alertas es autoritativa: se actualizan las presentes y se resuelven las ausentes de ese servidor.
7. Para error o pending, se retienen alertas previas como stale/no confirmadas.
8. Responder 2xx después del commit; errores de validación/cuerpo producen 4xx sin mutar el estado.

complete de raíz representa completitud de consultas Thruk, no conectividad del emisor. La fuente puede estar conectada y enviar complete false. El silencio del emisor se deriva por separado usando received_at.

## Persistencia SQLite propuesta

| Tabla | Clave y propósito |
| --- | --- |
| sources | ID local, `client_source_id` UUID estable v2, IP observada, primera/última recepción, versión de contrato, timestamp del último payload y revisión local. Emisores v2 quedan separados aunque compartan IP; clientes v1 mantienen la clave histórica source + IP. `deleted_at` conserva la referencia del historial de notificaciones. Frescura deriva de `last_received_at`; mensajes test no la alteran. |
| latest_snapshots | Un registro por source_id con el snapshot más reciente, `snapshot_id`, summary, timestamp, recepción, complete y hash canónico. El cuerpo recibido se conserva comprimido en `payload_gzip`; `payload_json` queda vacío por compatibilidad de esquema. |
| snapshot_receipts | Ventana de los últimos 256 IDs v2 por fuente y su hash/timestamp para deduplicar reintentos y rechazar un ID reutilizado con distinto contenido. |
| monitored_servers | Una fila por source_id + server; último estado ok/error/pending, última recepción de estado, última consulta ok y revisión local del snapshot que confirmó ese estado. El Gateway no expone detalle del error. |
| active_alerts | Una fila por source_id + server + backend + type + host + service. Guarda `status_information`, `visible_in_desktop`, first_seen_at y last_seen_at. La visibilidad no pertenece al estado de notificación. `stale` se deriva del servidor y la frescura de fuente. |
| monitored_statuses | Estados UP de snapshots históricos v1 con clave compuesta. V2 es alert-only; en la primera recepción v2 de una fuente se retiran sus UP heredados sin generar transiciones. Se mantiene separado de alertas, historial y notificaciones. |
| alert_history | Transiciones observadas: apertura, cambio de estado, cambio de hard/acknowledged/downtime y recuperación. Guarda clave de alerta, valores anterior/nuevo y observed_at del servidor web. |
| notification_settings, notification_channels, notification_rules, notification_rule_channels | Configuración global, ajustes de proveedor, secretos cifrados, filtros/plantillas y asociaciones de reglas con canales. Los IDs locales se usan en SQLite, pero el JSON de backup relaciona reglas y canales con claves portables propias del archivo. |
| notification_config_backups | Hasta 10 copias locales previas a una restauración. Solo contienen configuración; secretos cifrados con la clave externa `notification.key`. No forman parte del JSON portable ni incluyen datos de monitoreo o historial de entregas. |
| notification_events, notification_deliveries, notification_attempts, notification_suppression_state | Eventos y ejecución del motor. No se incluyen en backups portables; restaurar configuración conserva eventos/entregas históricos existentes, cancela pendientes y reemplaza las asociaciones actuales. |
| schema_version | Versión local del esquema para evolución controlada. |

Nombres configurados de servidor/backend y display name pueden cambiar; la clave no es un ID inmutable garantizado por Thruk. Un cambio puede parecer una alerta cerrada y otra nueva.

### Deduplicación y reconciliación

- Actualizar last_received_at aunque el snapshot sea idéntico.
- Calcular y almacenar el hash del contenido normalizado sin el timestamp raíz. Los POST idénticos refrescan recepción sin duplicar filas de alertas ni eventos de historial; el backend todavía reconcilia y escribe el snapshot actual en SQLite.
- Comparar el `timestamp` UTC raíz antes de aplicar cada snapshot. Una recepción más antigua se acepta sin error, actualiza la hora observada de recepción y no reemplaza el último snapshot ni el estado persistido.
- Comparar por clave compuesta. Crear historia por presencia, estado o cambio de flags relevantes. Cambio de last_check no crea por sí solo un evento de estado.
- Validar listas de identidad con conjuntos, sin crear una segunda copia completa del snapshot en diccionarios Python. Para estados UP, cargar una tabla temporal con `executemany` y reconciliar con UPDATE/INSERT/DELETE por conjuntos dentro de la transacción. La búsqueda normalizada se actualiza junto a cada estado y evita invocar un UDF Python por cada fila consultada.
- Para cada servidor `ok`, reconciliar `alerts[]` RAW. En v1, procesar también `ok_statuses[]` si aparece; si se omite, preservar filas UP heredadas. Al pasar una fuente a v2, quitar esas filas UP una vez sin producir eventos de recuperación.
- Actualizar `visible_in_desktop` en la alerta existente sin crear cambios de estado, historial, eventos o notificaciones. Dashboard Desktop usa esa marca por defecto y puede pedir la vista RAW.
- Aceptar `snapshot_id` v2 exactamente una vez por contenido; un reintento igual refresca tráfico, pero no avanza la revisión ni ejecuta reconciliación. Un ID reutilizado con distinto hash se rechaza con 409.
- Actualizar `status_information` cuando viene en el elemento, incluso si vale `""`; si una versión anterior omite el campo, preservar el texto almacenado. Los cambios de ese campo no son transiciones de alerta.
- En servidor ok, alerta ausente se registra recuperada a la hora de observación web, no como instante real de recuperación.
- Nunca recuperar alertas por servidores error/pending, por servidores ausentes en snapshot incompleto ni por silencio del emisor.
- Si complete true y un servidor conocido ya no aparece, marcarlo retirado/no configurado y conservar historial; no declarar recuperadas sus alertas solo porque se quitó de la configuración.
- Comparar revisiones locales para reconocer servidores omitidos de un snapshot parcial, incluso si dos recepciones tienen el mismo timestamp de milisegundos. Al superar el umbral de silencio, marcar fuente y alertas stale, conservando datos.
- El umbral debe configurarse localmente: Desktop no envía su intervalo, que puede llegar a 24 horas.
- Aplicar retención configurable al historial para evitar crecimiento ilimitado; definir el valor al implementar.

## API propuesta

No hay autenticación en dashboard, receptor ni APIs. Las rutas usan mismo origen y no requieren CORS.

| Método y ruta | Función |
| --- | --- |
| GET / | Dashboard visual servido directamente, sin autenticación. |
| POST /api/v1/ingest | Recibir test o snapshot. Sin autenticación; agrupación provisional por source + peer IP observada. |
| GET /api/v1/stats | Totales de alertas, agrupación por estado y fuentes online/offline. |
| GET /api/v1/sources | Fuentes, última recepción y estado de sus servidores. |
| DELETE /api/v1/sources/{source_id} | Retirar manualmente una fuente que superó `SOURCE_INACTIVE_AFTER_SECONDS`; borra su snapshot, estados y datos actuales de monitoreo. No requiere autenticación. |
| GET /api/v1/alerts | Alertas actuales; filtros opcionales por source_id, server, state, type y stale, más paginación. |
| GET /api/v1/statuses | `view=desktop` entrega alertas marcadas visibles; `view=all` conserva todas las alertas RAW y UP heredadas v1. La API mantiene `all` por omisión por compatibilidad; dashboard inicia en `desktop`. Incluye `status_information`, `visible_in_desktop`, `stale`, filtros y paginación. |
| GET /api/v1/history | Transiciones observadas; filtros por source_id/server y límite de paginación. |
| POST /api/v1/notifications/backup/export | Descarga configuración versionada JSON; contraseña opcional cifra credenciales para migración. |
| POST /api/v1/notifications/backup/preview | Valida un archivo y devuelve cantidades/advertencias sin modificar SQLite. |
| POST /api/v1/notifications/backup/restore | Reemplaza settings, canales, reglas y asociaciones dentro de una transacción, previa copia local recuperable. |
| GET /api/v1/notifications/backup/previous; POST /api/v1/notifications/backup/restore-previous | Consulta y recupera la copia local más reciente antes de un reemplazo. |
| GET /healthz | Salud del proceso; comprueba versión y presencia de tablas requeridas mediante consultas pequeñas, sin recorrer el archivo completo ni datos sensibles. No certifica integridad completa ni capacidad de escritura del volumen. |

La UI local consulta `/healthz`, `/api/v1/statuses`, `/api/v1/stats` y `/api/v1/sources`. Inicia en la vista de problemas visibles en Desktop y ofrece RAW completo. Busca métricas y tabla contra la misma revisión con `monitoring_revision`; si cambia durante la lectura, vuelve a consultar. `alerts` en stats conserva conteos RAW globales y `desktop_visible_alerts` publica el alcance de Desktop. Las páginas, filtros y orden se resuelven en SQLite. Todos solicita bloques de 500 solo al desplazarse; no descarga la lista completa al cargar.

`/api/v1/stats` calcula conteos globales. `alerts` representa alertas RAW persistidas; `desktop_visible_alerts` suma los registros con `visible_in_desktop=1`; `monitoring` mantiene su alcance histórico al combinar alertas con UP heredados. Fuentes online/offline derivan de su último webhook. Una condición sin alertas solo se presenta saludable cuando las fuentes están recientes y cada servidor está `ok` en un snapshot completo.

`POST /api/v1/ingest` acepta snapshots v1 y v2, gzip estándar y JSON sin comprimir. Límites independientes: 8 MiB comprimidos, 64 MiB sin comprimir y 128 MiB descomprimidos por defecto. El middleware limita cada chunk antes de acumularlo. Responde 400 para gzip/JSON malformado, 413 al exceder límites, 415 ante codificación no admitida y 409 al reutilizar un snapshot ID con contenido diferente.

### Retiro manual de fuentes

`OFFLINE_AFTER_SECONDS` conserva el criterio de frescura. `SOURCE_INACTIVE_AFTER_SECONDS` habilita retiro manual sin purga automática. Las fuentes nuevas usan `client_source_id` estable; las legacy siguen identificadas por source + peer IP. El próximo snapshot válido de una fuente retirada vuelve a habilitarla.

El dashboard y la API no tienen autenticación. Cualquier cliente que alcance el puerto puede invocar la eliminación. Publica el servicio solo en loopback o una IP privada de confianza y restringe Windows Defender Firewall a los equipos/subredes necesarios. El botón y el diálogo de confirmación no sustituyen al firewall.

### Backup de configuración de notificaciones

El contrato versionado y los procedimientos de exportación/restauración están en [notification-backup-format.md](notification-backup-format.md). El archivo contiene solo configuración de Notifications. Una copia cifrada protege credenciales con Fernet y PBKDF2-HMAC-SHA256; al importarla, los secretos se vuelven a cifrar con la clave de la instalación de destino. Las restauraciones guardan hasta 10 puntos locales previos en `notification_config_backups`, cifrando sus secretos con `notification.key`. El reemplazo no importa alertas/eventos ni crea entregas; conserva el monitoreo e historial existentes, cancela entregas pendientes y actualiza las referencias de configuración dentro de una transacción.

## Docker Compose y red

Publicar el puerto web y webhook, con volumen SQLite persistente. El contenedor escucha en 0.0.0.0, pero publicación hacia Windows debe permitir elegir la IP host. Loopback como valor por defecto; para recibir desde otra computadora, publicar en una IP LAN privada del host y limitar el firewall de Windows al segmento confiable. No abrir más puertos.

Desktop usa http://<IP-LAN-host>:<puerto>/api/v1/ingest. No se colocan tokens, contraseñas ni etiquetas obligatorias en la ruta.

### Precauciones sin autenticación

- No configurar port forwarding/NAT desde Internet.
- No publicar el puerto en una interfaz pública. Mantener loopback por defecto; cuando haga falta, usar solo la IP privada necesaria.
- Limitar Windows Defender Firewall al rango o VLAN confiable y revisar reglas de Docker Desktop.
- Segmentar la red de monitoreo de redes de invitados/dispositivos no confiables. Cualquier cliente con acceso al puerto puede falsificar datos y leer dashboard/API.
- No confiar en IP como autenticación. Puede cambiar, compartirse por NAT o identificar solo un proxy, y no prueba quién envió el JSON.
- HTTP no cifra tráfico; operar solo en LAN controlada. VPN/HTTPS podrían evaluarse como protección de red en otra etapa, sin agregar login.
- No almacenar secretos recibidos; el Gateway no los envía.
- Las APIs no requieren autenticación; CORS no es una barrera de seguridad y no sustituye firewall o segmentación de red. El dashboard inserta contenido recibido como texto y las consultas SQLite parametrizan valores.
- Los webhooks tienen un límite configurable de 8 MiB y validación estricta de tipos y relaciones. El receptor trata todo contenido como datos y no lo ejecuta.

## Decisiones

| Decisión | Motivo |
| --- | --- |
| FastAPI + SQLite, una app | Pocos servicios y mantenimiento bajo para volumen bajo/moderado. |
| HTML/CSS/JS local | Uso offline, sin build chain frontend ni CDN. |
| Snapshot actual separado del historial | No duplicar alerta cada intervalo; retener cambios observables. |
| Transacción por recepción | Mantener consistente el snapshot, estado de servidor y alertas. |
| Peer IP observada como agrupador provisional | Mantiene el endpoint exacto solicitado sin alterar Desktop; es débil y no autentica. |
| Sin login, keys, tokens, sesiones ni roles | Requisito explícito; límite de confianza es firewall/red aislada. |
| Alertas stale durante fallo/silencio | Diferenciar cero problemas de falta de datos. |
| No cambiar Desktop | Web se adapta al contrato real; mejoras opcionales quedan para después. |

## Pruebas de backend

1. Usar fixtures contrastados con la serialización real y los ejemplos de webhook-contract.md.
2. Verificar snapshots válidos, host/service, timestamps, estados, acknowledgement y downtime; rechazar JSON malformado, raíz inesperada y cuerpos grandes.
3. Verificar test: respuesta 2xx sin cambiar snapshot ni alertas.
4. Verificar snapshots idénticos: recepción actualizada sin eventos duplicados.
5. Verificar alerta nueva, cambio de estado/flags y recuperación observada desde servidor ok.
6. Verificar complete false con servidores ok + error/pending: reconciliar solo ok y conservar stale el resto.
7. Distinguir snapshot completo vacío de fuente silenciosa y servidor error.
8. Verificar la agrupación provisional por peer IP y documentar que no distingue instancias detrás de una misma IP.
9. Verificar umbral stale configurable, persistencia tras reiniciar, filtros/paginación, healthcheck con esquema válido y rechazo de esquema ausente.
10. Forzar timestamps de recepción iguales y POST simultáneos para comprobar que las revisiones serializadas mantienen stale los servidores omitidos por el último snapshot parcial y que la hora de recepción no retrocede.
11. Actualizar bases v1/v2 para comprobar que se preserva el stale derivado de timestamps y revisiones ya guardadas.
12. Confirmar que no hay solicitudes externas del frontend y que el uso no depende de Internet/CDN.
13. La suite Python se ejecuta dentro del contenedor de tests con Compose. `tests/test-container-persistence.ps1` inicia un proyecto temporal, envía un fixture, reinicia y recrea el contenedor, genera una copia online de SQLite y restaura el fixture. No usar producción.

Los comandos de operación y prueba están en [README.md](../README.md). Las pruebas usan datos ficticios y no contactan sistemas productivos.
