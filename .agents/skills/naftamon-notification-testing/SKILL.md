---
name: naftamon-notification-testing
description: Valida reglas, cola SQLite, proveedores simulados, seguridad y GUI de Notifications en NaftaMon Web mediante Docker.
---

# Pruebas de notificaciones

Aplica [el skill web de pruebas](../naftamon-web-testing/SKILL.md) y [el diseño](../../../web/docs/notification-design.md). Usa un proyecto Compose y volumen de pruebas aislados del despliegue del usuario. Simula Telegram, Gotify, ntfy y HTTP; nunca emplees tokens ni destinos reales. Comprueba que el dry-run no envía, que un POST de ingesta no espera al proveedor, que snapshots idénticos y fallos Thruk no crean entregas, y que 429/timeout/reinicio respetan estado persistente. Inspecciona dashboard y formularios en navegador cuando esté disponible. Reporta qué pruebas se ejecutaron realmente y cualquier limitación.
