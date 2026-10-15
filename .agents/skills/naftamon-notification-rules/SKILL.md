---
name: naftamon-notification-rules
description: Diseña o modifica reglas y eventos de notificación de NaftaMon Web respetando el contrato real del Gateway y la reconciliación SQLite.
---

# Reglas de NaftaMon Web

Lee [el contrato](../../../web/docs/webhook-contract.md) y [el diseño](../../../web/docs/notification-design.md) antes de cambiar filtros o eventos.

Usa únicamente campos recibidos; no inventes plugin output ni identificador de emisor. Una recuperación exige snapshot autoritativo `ok` del servidor correspondiente. Un snapshot repetido, un test, un servidor `error`/`pending` o una fuente silenciosa no deben disparar alertas nuevas ni recuperaciones. Conserva identidad por episodio para permitir una nueva apertura tras recuperación. Evalúa condiciones con operadores acotados, sin ejecutar expresiones del usuario. Prueba AND/OR, campos ausentes, cooldown, recordatorios y cambios de reglas sin reenvío masivo.
