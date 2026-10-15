---
name: naftamon-notification-providers
description: Implementa y revisa destinos Telegram, Gotify, ntfy y webhook HTTP de NaftaMon Web, incluyendo envío seguro, secretos y reintentos.
---

# Proveedores de notificaciones

Lee [el diseño](../../../web/docs/notification-design.md) para la interfaz de adaptadores y la política de destinos. Mantén la ingesta independiente de la red: el worker envía desde la cola persistente. Clasifica timeout, red, 429 y 5xx como transitorios; limita intentos y respeta `Retry-After`. Registra errores saneados, nunca URLs con secretos ni cuerpos de respuesta sin filtrar. Tokens cifrados requieren clave externa al volumen de datos. El API solo informa si hay secreto configurado.

Al añadir URLs de destino, valida esquema, host, resolución y cada dirección de conexión. Bloquea loopback, link-local y metadatos; admite LAN solo por rango configurado explícitamente. Desactiva redirecciones y proxies. Prueba con servidores simulados locales y no envíes mensajes reales durante tests.
