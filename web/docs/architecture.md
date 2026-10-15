# NaftaMon Web — arquitectura y estado de implementación

## Estado

La etapa 1 definió esta arquitectura. La etapa 2 ya implementa receptor, API, persistencia SQLite y Compose; el dashboard visual completo sigue pendiente. NaftaMon Desktop no se modificó. El contrato real del receptor está en [webhook-contract.md](webhook-contract.md).

## Arquitectura

El objetivo es una aplicación monolítica FastAPI servida por un contenedor Docker Compose. La etapa 2 implementa el receptor y las APIs JSON; la interfaz visual se añadirá después con HTML, CSS y JavaScript locales. SQLite vive en un volumen persistente. No hacen falta React, Redis, PostgreSQL, Kubernetes, CDN ni servicios externos.

El mismo proceso sirve dashboard, APIs de lectura y el receptor HTTP. CSS y JavaScript se incluyen localmente. No se publica SQLite como servicio de red. Para el tráfico esperado, usar Uvicorn con un worker, imagen Python pequeña, usuario no privilegiado y healthcheck HTTP.

## Estructura actual y futura

Backend y pruebas actuales más las ubicaciones previstas para el futuro dashboard:

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
  tests/
    conftest.py
    test_backend.py
    test-container-persistence.ps1
  docs/
    architecture.md
    webhook-contract.md
~~~

Mantener módulos pequeños por responsabilidad y acceso SQLite sencillo. Incorporar ORM o migraciones externas solo si el crecimiento lo justifica.

## Ingesta y semántica

1. Desktop hace POST JSON a la URL configurada en NaftaMon.
2. FastAPI valida tamaño, JSON y estructura del tipo de mensaje. Toda entrada se considera no confiable.
3. El endpoint requerido es `POST /api/v1/ingest`, sin etiqueta ni credencial obligatoria. Como el contrato no tiene instance_id, esta etapa agrupa por el campo `source` más la IP observada del peer TCP. Es una heurística débil: NAT, proxies y varias instalaciones en una misma computadora pueden mezclar emisores. No es autenticación.
4. Los mensajes de prueba se aceptan aparte; no actualizan snapshot ni alertas.
5. Los snapshots periódicos se guardan y reconcilian en una transacción SQLite.
6. Por cada servidor ok, su lista de alertas es autoritativa: se actualizan las presentes y se resuelven las ausentes de ese servidor.
7. Para error o pending, se retienen alertas previas como stale/no confirmadas.
8. Responder 2xx después del commit; errores de validación/cuerpo producen 4xx sin mutar el estado.

complete de raíz representa completitud de consultas Thruk, no conectividad del emisor. La fuente puede estar conectada y enviar complete false. El silencio del emisor se deriva por separado usando received_at.

## Persistencia SQLite propuesta

| Tabla | Clave y propósito |
| --- | --- |
| sources | ID local, valor source recibido, IP observada del peer como agrupador provisional, primera/última recepción, último timestamp de payload y completitud del último snapshot. No hay columna instance_id en el contrato actual; si Desktop la incorpora, se añadirá con una migración. Frescura derivada de last_received_at y umbral local configurable. Los mensajes test no crean/actualizan una fuente ni su frescura. |
| latest_snapshots | Un registro por source_id con JSON periódico más reciente, timestamp del Gateway, received_at, complete y hash canónico. Tests no reemplazan este registro. No guardar una fila por cada intervalo idéntico. |
| monitored_servers | Una fila por source_id + server; último estado ok/error/pending, última recepción de estado y última consulta ok. El Gateway no expone detalle del error. |
| active_alerts | Una fila por source_id + server + backend + type + host + service. Guarda campos de alerta, first_seen_at y last_seen_at; `stale` se deriva del estado del servidor y la frescura de fuente. No usar state como parte de la clave. |
| alert_history | Transiciones observadas: apertura, cambio de estado, cambio de hard/acknowledged/downtime y recuperación. Guarda clave de alerta, valores anterior/nuevo y observed_at del servidor web. |
| schema_version | Versión local del esquema para evolución controlada. |

Nombres configurados de servidor/backend y display name pueden cambiar; la clave no es un ID inmutable garantizado por Thruk. Un cambio puede parecer una alerta cerrada y otra nueva.

### Deduplicación y reconciliación

- Actualizar last_received_at aunque el snapshot sea idéntico.
- Calcular hash del contenido normalizado sin el timestamp raíz para evitar reescrituras innecesarias del snapshot; recepción sí se actualiza.
- Comparar por clave compuesta. Crear historia por presencia, estado o cambio de flags relevantes. Cambio de last_check no crea por sí solo un evento de estado.
- En servidor ok, alerta ausente se registra recuperada a la hora de observación web, no como instante real de recuperación.
- Nunca recuperar alertas por servidores error/pending, por servidores ausentes en snapshot incompleto ni por silencio del emisor.
- Si complete true y un servidor conocido ya no aparece, marcarlo retirado/no configurado y conservar historial; no declarar recuperadas sus alertas solo porque se quitó de la configuración.
- Al superar el umbral de silencio, marcar fuente y alertas stale, conservando datos.
- El umbral debe configurarse localmente: Desktop no envía su intervalo, que puede llegar a 24 horas.
- Aplicar retención configurable al historial para evitar crecimiento ilimitado; definir el valor al implementar.

## API propuesta

No hay autenticación en dashboard, receptor ni APIs. Las rutas usan mismo origen y no requieren CORS.

| Método y ruta | Función |
| --- | --- |
| GET / | Mensaje de estado básico; el dashboard visual está pendiente. |
| POST /api/v1/ingest | Recibir test o snapshot. Sin autenticación; agrupación provisional por source + peer IP observada. |
| GET /api/v1/stats | Totales de alertas, agrupación por estado y fuentes online/offline. |
| GET /api/v1/sources | Fuentes, última recepción y estado de sus servidores. |
| GET /api/v1/alerts | Alertas actuales; filtros opcionales por source_id, server, state, type y stale, más paginación. |
| GET /api/v1/history | Transiciones observadas; filtros por source_id/server y límite de paginación. |
| GET /healthz | Salud del proceso y SQLite, sin datos sensibles. |

Respuestas de POST sugeridas: 202 tras aceptar y persistir; 400 JSON/estructura inválida; 413 cuerpo demasiado grande; 415 tipo de contenido no soportado. Límite inicial sugerido: 8 MiB configurable.

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
9. Verificar umbral stale configurable, persistencia tras reiniciar, filtros/paginación y healthcheck.
10. Confirmar que no hay solicitudes externas del frontend y que el uso no depende de Internet/CDN.
11. La suite Python se ejecuta dentro del contenedor de tests con Compose. `tests/test-container-persistence.ps1` inicia un proyecto temporal, envía un fixture, reinicia el contenedor real y verifica que la alerta persiste en el volumen. No usar producción.

Los comandos de operación y prueba están en [README.md](../README.md). Las pruebas usan datos ficticios y no contactan sistemas productivos.
