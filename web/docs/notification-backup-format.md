# Backup de configuración de notificaciones

NaftaMon Web exporta solo la configuración necesaria para reconstruir Notifications. El archivo no incluye alertas, estados de monitoreo, fuentes ni el historial de entregas. Los identificadores SQLite no se exportan: las reglas apuntan a claves portables locales al documento, como `channel-0001`.

## Esquema JSON, versión 1

```json
{
  "format": "naftamon-notifications-backup",
  "schema_version": 1,
  "exported_at": "2026-10-16T14:30:00Z",
  "notifications": {
    "settings": { "enabled": true, "history_retention_days": 90 },
    "channels": [
      {
        "key": "channel-0001",
        "name": "Operaciones",
        "provider": "webhook",
        "config": {
          "url": "https://hooks.example.test/naftamon",
          "method": "POST",
          "content_type": "application/json",
          "header_names": ["X-Notify-Token"],
          "json_template": { "host": "{host}", "state": "{state}" }
        },
        "enabled": true,
        "timeout_seconds": 10,
        "rate_limit_seconds": 1,
        "secret_configured": true
      }
    ],
    "rules": [
      {
        "name": "Disco crítico",
        "description": "",
        "enabled": true,
        "conditions": {
          "operator": "AND",
          "conditions": [
            { "field": "state", "operator": "equals", "value": "CRITICAL" },
            { "field": "status_information", "operator": "contains", "value": "DISK" }
          ]
        },
        "events": ["opened", "state_changed"],
        "channel_keys": ["channel-0001"],
        "cooldown_seconds": 0,
        "reminder_interval_seconds": null,
        "subject_template": "{event_type}: {host}",
        "body_template": "{server} {service} {state}"
      }
    ]
  }
}
```

`channels[].config` contiene los campos públicos validados del proveedor y sus plantillas. `secret_configured` indica si hay credenciales sin revelar su contenido. `rules[].channel_keys` mantiene las relaciones entre reglas y canales aunque cambien sus IDs internos. Las condiciones y plantillas se validan con las mismas reglas que los formularios de Settings.

## Secretos

**Exportar configuración** crea un JSON sin contraseñas, tokens ni cabeceras secretas. Durante la restauración, cualquier canal que indique `secret_configured: true` queda deshabilitado hasta que se reintroduzcan sus credenciales. El preview avisa qué canales se verán afectados.

**Exportar copia cifrada** incluye los secretos en `secrets_encrypted`, nunca en texto claro. Versión 1 usa `fernet-pbkdf2-sha256`, PBKDF2-HMAC-SHA256 con 600.000 iteraciones, salt aleatoria de 16 bytes y Fernet para cifrado autenticado. La contraseña no se almacena; el navegador la envía al endpoint local de NaftaMon Web solo para esa solicitud. Como esta versión usa HTTP, realiza la operación únicamente desde una red de confianza. Se exigen al menos 12 caracteres. Conserva la contraseña aparte del archivo: si se pierde, el backup cifrado no puede abrirse.

En la instalación de destino, los secretos se descifran en memoria y vuelven a cifrarse con la clave local `notification.key`. La copia cifrada se puede migrar a otra instalación y no requiere migrar esa clave. La clave local sí debe conservarse para que funcione la configuración restaurada.

## Validación y restauración

La interfaz **Settings → Notifications** permite seleccionar un JSON, introducir su contraseña si está cifrado y revisar una vista previa antes de reemplazar la configuración. Se rechazan JSON inválido, claves JSON repetidas, formato o versión desconocidos, referencias rotas, proveedores inválidos, valores fuera de rango y archivos mayores de 2 MiB. No se permiten campos adicionales en el contrato.

El reemplazo valida todo antes de escribir, guarda primero hasta 10 copias locales previas y luego actualiza settings, canales, reglas y asociaciones en una transacción SQLite. Si falla, se revierte la transacción y queda la copia anterior disponible. Las copias previas contienen solo configuración y secretos cifrados con la clave local; se guardan en la base persistente y se pueden recuperar desde la misma sección. Para recuperarlas debe seguir disponible `notification.key`.

El reemplazo cancela las entregas pendientes/en curso registradas, pero no genera eventos ni nuevas notificaciones. Una solicitud que el worker ya hubiera enviado al proveedor no se puede retirar. Conserva las alertas, estados de monitoreo y eventos/historial ya presentes en la base. Las entregas históricas conservan nombres y resultados; sus referencias a IDs de reglas/canales reemplazadas pueden quedar vacías. El archivo importado nunca restaura el historial.

## Endpoints

Todas las rutas continúan sin autenticación, de acuerdo con el diseño actual. Úsalas solo en una LAN confiable y limita el puerto mediante firewall.

| Ruta | Uso |
| --- | --- |
| `POST /api/v1/notifications/backup/export` | Recibe `{ "password": null }` para exportar sin secretos o una contraseña de 12+ caracteres para cifrar secretos. Devuelve JSON descargable con nombre fechado. |
| `POST /api/v1/notifications/backup/preview` | Recibe `{ "backup": {...}, "password": null }`; valida y devuelve conteos/advertencias sin aplicar cambios. |
| `POST /api/v1/notifications/backup/restore` | Mismos campos que preview más `"confirm_replace": true`; crea copia previa y reemplaza transaccionalmente. |
| `GET /api/v1/notifications/backup/previous` | Resume la copia local previa más reciente, si existe. |
| `POST /api/v1/notifications/backup/restore-previous` | Recibe `{ "confirm_replace": true }` y recupera esa copia. La configuración actual se conserva primero como otro punto de recuperación. |
