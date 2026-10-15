---
name: naftamon-web-docker
description: Construye, ejecuta e inspecciona el futuro servicio self-hosted NaftaMon Web con su Compose aislado y datos SQLite persistentes.
---

# Docker para NaftaMon Web

Lee [web/AGENTS.md](../../../web/AGENTS.md) y [web/docs/architecture.md](../../../web/docs/architecture.md) antes de cambiar Compose o el despliegue.

- Usa el Compose propio de web/; no mezcles el stack web con compose.dev.yaml del Desktop.
- Mantén SQLite en un volumen persistente y no publiques el archivo de base de datos.
- No agregues servicios externos, credenciales, login, API keys ni tokens.
- Evita dependencia de Internet/CDNs para la ejecución del dashboard; las dependencias de build deben quedar fijadas/documentadas.
- Por defecto expón el puerto solo en loopback. Para recibir desde otra computadora, usa una dirección LAN privada y documenta una regla de Windows Defender Firewall limitada al segmento confiable.
- No abras port forwarding ni expongas el receptor a Internet. La etiqueta de ruta del webhook no es un secreto.
- Usa solo fixtures/mock y datos inventados; no apuntes a producción.
- Al reportar, incluye comandos exactos, estado real de los contenedores, persistencia verificada y cualquier comprobación no ejecutada.
