---
name: naftamon-web-testing
description: Diseña y ejecuta pruebas del contrato webhook, reconciliación SQLite, frescura, APIs y dashboard de NaftaMon Web.
---

# Pruebas de NaftaMon Web

Lee [web/AGENTS.md](../../../web/AGENTS.md), [web/docs/webhook-contract.md](../../../web/docs/webhook-contract.md) y [web/docs/architecture.md](../../../web/docs/architecture.md).

Cubre los invariantes observables:

- Fixtures válidos basados en el contrato real: snapshot completo con alerta, snapshot completo vacío, snapshot parcial con ok/error/pending y mensaje test.
- El mensaje test no reemplaza el snapshot ni altera alertas.
- Repetir un snapshot idéntico refresca recepción sin duplicar filas/eventos.
- Una fuente etiquetada no mezcla datos con otra aunque compartan nombres de servidores.
- Servidor ok reconcilia altas, cambios y recuperaciones observadas; error/pending conserva alertas stale.
- Fuente silenciosa queda stale sin marcar alertas como recuperadas. Diferenciar esto de complete true con alerts vacío.
- Reinicio del contenedor conserva SQLite; tamaño, JSON inválido, filtros y paginación se verifican.
- APIs y dashboard siguen sin autenticación y no descargan activos externos.

Ejecuta las pruebas dentro del Compose web cuando exista. Usa exclusivamente datos ficticios y mock; no uses servicios productivos. Reporta PASS/FAIL/NOT TESTED con comandos ejecutados y no atribuyas a un test propiedades que no comprobó.
