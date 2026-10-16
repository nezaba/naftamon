# Instrucciones para web/

Mantener aplicación, configuración, pruebas y documentación web dentro de web/. No modificar archivos de NaftaMon Desktop ni su Webhook Gateway.

Antes de implementar ingestión, leer [el contrato real del Gateway](docs/webhook-contract.md) y [la arquitectura propuesta](docs/architecture.md). Actualizar arquitectura cuando una decisión cambie.

Restricciones de esta versión:

- No implementar autenticación: sin usuarios, passwords, API keys, bearer tokens, sesiones, OAuth ni roles en dashboard, receptor o APIs.
- No añadir credenciales Thruk ni destinos productivos en pruebas. Los proveedores de notificaciones opcionales se configuran por el operador; usar mocks locales para validarlos.
- No depender de CDN ni de conexión a Internet para usar el dashboard.
- Diferenciar cero alertas de fuente stale y de servidor error/pending.
- Reconciliar solo snapshots autoritativos de servidores ok; nunca resolver alertas por fallos o silencio.
- El receptor público de esta etapa es `POST /api/v1/ingest`, sin credenciales ni headers obligatorios.
- Como el JSON no identifica una instalación, se agrupa provisionalmente por `source` y la IP observada del peer TCP. Es una heurística débil, no autenticación; no confiar en `X-Forwarded-For` ni en la IP para seguridad.
- Publicar solo en loopback o IP privada escogida, y restringir Windows Firewall a red confiable.
- La eliminación manual de fuentes no tiene autenticación de aplicación en esta versión; no publicar su API fuera de una LAN confiable y limitar el puerto con el firewall del host.

Skills locales:

- Contrato: ../.agents/skills/naftamon-webhook-contract/SKILL.md
- Docker: ../.agents/skills/naftamon-web-docker/SKILL.md
- Pruebas: ../.agents/skills/naftamon-web-testing/SKILL.md
- Reglas: ../.agents/skills/naftamon-notification-rules/SKILL.md
- Proveedores: ../.agents/skills/naftamon-notification-providers/SKILL.md
- Pruebas de notificaciones: ../.agents/skills/naftamon-notification-testing/SKILL.md

El proyecto incluye receptor, API de lectura, persistencia SQLite y dashboard local en HTML/CSS/JavaScript servido desde FastAPI. Mantener el dashboard sin recursos externos y sin autenticación. El dashboard debe consumir solo APIs del mismo origen y distinguir frescura de fuente de estado de consulta por servidor.

Al cambiar UI, verificar `/`, `/static/dashboard.css` y `/static/dashboard.js` mediante pruebas web. Para comprobaciones integrales usa `docker compose` de este directorio y fixtures inventados; nunca uses la red de producción. Inspecciona la UI con navegador cuando esté disponible y registra las comprobaciones que no puedan ejecutarse.
