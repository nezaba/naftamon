from __future__ import annotations

import base64
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .channel_validation import ChannelValidationError, validate_channel_configuration, validate_secrets
from .crypto import SecretStoreUnavailable, decrypt_secrets, encrypt_secrets
from .rules import RuleValidationError, validate_conditions, validate_events, validate_template


BACKUP_FORMAT = "naftamon-notifications-backup"
BACKUP_SCHEMA_VERSION = 1
BACKUP_MAX_BYTES = 2 * 1024 * 1024
BACKUP_REQUEST_MAX_BYTES = BACKUP_MAX_BYTES + 8192
BACKUP_MAX_CHANNELS = 500
BACKUP_MAX_RULES = 2000
BACKUP_KDF_ITERATIONS = 600_000
BACKUP_ALGORITHM = "fernet-pbkdf2-sha256"
_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class BackupValidationError(ValueError):
    pass


def _now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _object(value: Any, label: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BackupValidationError(f"{label} debe ser un objeto JSON.")
    if set(value) != keys:
        raise BackupValidationError(f"{label} tiene campos faltantes o desconocidos.")
    return value


def _string(value: Any, label: str, maximum: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise BackupValidationError(f"{label} debe ser texto de hasta {maximum} caracteres.")
    return value


def _bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise BackupValidationError(f"{label} debe ser booleano.")
    return value


def _integer(value: Any, label: str, minimum: int, maximum: int, *, optional: bool = False) -> int | None:
    if optional and value is None:
        return None
    if type(value) is not int or not minimum <= value <= maximum:
        raise BackupValidationError(f"{label} debe ser un entero entre {minimum} y {maximum}.")
    return value


def _derive_backup_key(password: str, salt: bytes, iterations: int = BACKUP_KDF_ITERATIONS) -> bytes:
    return base64.urlsafe_b64encode(
        PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations)
        .derive(password.encode("utf-8"))
    )


def _encrypt_secrets_bundle(value: dict[str, dict[str, Any]], password: str) -> dict[str, Any]:
    _string(password, "La contraseña del backup", 1024)
    if len(password) < 12:
        raise BackupValidationError("La contraseña del backup debe tener al menos 12 caracteres.")
    salt = secrets.token_bytes(16)
    key = _derive_backup_key(password, salt)
    plaintext = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    token = Fernet(key).encrypt(plaintext).decode("ascii")
    return {
        "algorithm": BACKUP_ALGORITHM,
        "iterations": BACKUP_KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "ciphertext": token,
    }


def _decrypt_secrets_bundle(value: Any, password: str | None) -> dict[str, dict[str, Any]]:
    envelope = _object(value, "secrets_encrypted", {"algorithm", "iterations", "salt", "ciphertext"})
    if envelope["algorithm"] != BACKUP_ALGORITHM or envelope["iterations"] != BACKUP_KDF_ITERATIONS:
        raise BackupValidationError("El algoritmo o la versión de cifrado del backup no es compatible.")
    if type(envelope["iterations"]) is not int:
        raise BackupValidationError("La configuración de cifrado del backup no es válida.")
    if password is None:
        raise BackupValidationError("Este backup está cifrado; introduce su contraseña.")
    _string(password, "La contraseña del backup", 1024)
    if len(password) < 12:
        raise BackupValidationError("La contraseña del backup debe tener al menos 12 caracteres.")
    try:
        salt = base64.b64decode(envelope["salt"], validate=True)
        if len(salt) != 16:
            raise ValueError("invalid salt")
        ciphertext = envelope["ciphertext"]
        if not isinstance(ciphertext, str) or len(ciphertext) > BACKUP_MAX_BYTES * 2:
            raise ValueError("invalid ciphertext")
        raw = Fernet(_derive_backup_key(password, salt)).decrypt(ciphertext.encode("ascii"))
        decoded = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, InvalidToken, json.JSONDecodeError, TypeError, RecursionError):
        raise BackupValidationError("No se pudo descifrar el backup: verifica la contraseña y el archivo.") from None
    if not isinstance(decoded, dict) or any(not isinstance(v, dict) for v in decoded.values()):
        raise BackupValidationError("El contenido cifrado del backup no tiene una estructura válida.")
    return decoded


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise ValueError("invalid JSON number")


def parse_json_document(raw: bytes) -> dict[str, Any]:
    if len(raw) > BACKUP_REQUEST_MAX_BYTES:
        raise BackupValidationError("El archivo de configuración supera el límite de 2 MiB.")
    try:
        parsed = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant)
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise BackupValidationError("El archivo no contiene JSON UTF-8 válido o repite claves.") from None
    if not isinstance(parsed, dict):
        raise BackupValidationError("La raíz del backup debe ser un objeto JSON.")
    return parsed


def export_document(db, password: str | None = None) -> dict[str, Any]:
    settings = db.execute(
        "SELECT enabled, history_retention_days FROM notification_settings WHERE id = 1"
    ).fetchone()
    channels = db.execute("SELECT * FROM notification_channels ORDER BY id").fetchall()
    if len(channels) > BACKUP_MAX_CHANNELS:
        raise BackupValidationError("Hay demasiados canales para exportar en un solo archivo.")
    channel_keys: dict[int, str] = {}
    channel_documents: list[dict[str, Any]] = []
    secret_bundle: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(channels, start=1):
        key = f"channel-{index:04d}"
        channel_keys[row["id"]] = key
        has_secret = bool(row["secrets_ciphertext"])
        channel_documents.append({
            "key": key,
            "name": row["name"],
            "provider": row["provider"],
            "config": json.loads(row["config_json"]),
            "enabled": bool(row["enabled"]),
            "timeout_seconds": row["timeout_seconds"],
            "rate_limit_seconds": row["rate_limit_seconds"],
            "secret_configured": has_secret,
        })
        if has_secret:
            secret_bundle[key] = decrypt_secrets(row["secrets_ciphertext"])

    rules = db.execute("SELECT * FROM notification_rules ORDER BY id").fetchall()
    if len(rules) > BACKUP_MAX_RULES:
        raise BackupValidationError("Hay demasiadas reglas para exportar en un solo archivo.")
    rule_documents = []
    for row in rules:
        linked = db.execute(
            "SELECT channel_id FROM notification_rule_channels WHERE rule_id = ? ORDER BY channel_id",
            (row["id"],),
        ).fetchall()
        rule_documents.append({
            "name": row["name"],
            "description": row["description"],
            "enabled": bool(row["enabled"]),
            "conditions": json.loads(row["conditions_json"]),
            "events": json.loads(row["events_json"]),
            "channel_keys": [channel_keys[item["channel_id"]] for item in linked if item["channel_id"] in channel_keys],
            "cooldown_seconds": row["cooldown_seconds"],
            "reminder_interval_seconds": row["reminder_interval_seconds"],
            "subject_template": row["subject_template"],
            "body_template": row["body_template"],
        })

    document: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "schema_version": BACKUP_SCHEMA_VERSION,
        "exported_at": _now_text(),
        "notifications": {
            "settings": {
                "enabled": bool(settings["enabled"]),
                "history_retention_days": settings["history_retention_days"],
            },
            "channels": channel_documents,
            "rules": rule_documents,
        },
    }
    if password is not None:
        document["secrets_encrypted"] = _encrypt_secrets_bundle(secret_bundle, password)
    return document


def validate_document(document: dict[str, Any], password: str | None) -> dict[str, Any]:
    expected_root = {"format", "schema_version", "exported_at", "notifications"}
    if "secrets_encrypted" in document:
        expected_root.add("secrets_encrypted")
    _object(document, "El backup", expected_root)
    if document["format"] != BACKUP_FORMAT:
        raise BackupValidationError("El archivo no es un backup de configuración de notificaciones NaftaMon.")
    if type(document["schema_version"]) is not int or document["schema_version"] != BACKUP_SCHEMA_VERSION:
        raise BackupValidationError("La versión de esquema del backup no es compatible con esta instalación.")
    exported_at = _string(document["exported_at"], "exported_at", 64)
    try:
        parsed_date = datetime.fromisoformat(exported_at.replace("Z", "+00:00"))
        if parsed_date.tzinfo is None:
            raise ValueError("timezone missing")
    except ValueError:
        raise BackupValidationError("exported_at debe ser una fecha ISO8601 con zona horaria.") from None

    notifications = _object(document["notifications"], "notifications", {"settings", "channels", "rules"})
    settings = _object(notifications["settings"], "notifications.settings", {"enabled", "history_retention_days"})
    normalized_settings = {
        "enabled": _bool(settings["enabled"], "notifications.settings.enabled"),
        "history_retention_days": _integer(settings["history_retention_days"], "history_retention_days", 0, 3650),
    }

    raw_channels = notifications["channels"]
    raw_rules = notifications["rules"]
    if not isinstance(raw_channels, list) or len(raw_channels) > BACKUP_MAX_CHANNELS:
        raise BackupValidationError("channels debe ser una lista de hasta 500 elementos.")
    if not isinstance(raw_rules, list) or len(raw_rules) > BACKUP_MAX_RULES:
        raise BackupValidationError("rules debe ser una lista de hasta 2000 elementos.")
    encrypted = "secrets_encrypted" in document
    decoded_bundle = _decrypt_secrets_bundle(document["secrets_encrypted"], password) if encrypted else {}
    if not encrypted and password is not None:
        raise BackupValidationError("Este backup no está cifrado; deja vacía la contraseña.")

    normalized_channels: list[dict[str, Any]] = []
    keys_to_channel: dict[str, dict[str, Any]] = {}
    channel_names: set[str] = set()
    channels_with_secret: set[str] = set()
    warnings: list[str] = []
    for index, item in enumerate(raw_channels):
        channel = _object(item, f"channels[{index}]", {
            "key", "name", "provider", "config", "enabled", "timeout_seconds", "rate_limit_seconds", "secret_configured",
        })
        key = _string(channel["key"], f"channels[{index}].key", 80)
        if not _KEY_PATTERN.fullmatch(key) or key in keys_to_channel:
            raise BackupValidationError("Las claves portables de canales deben ser únicas y válidas.")
        name = _string(channel["name"], f"channels[{index}].name", 100).strip()
        if name in channel_names:
            raise BackupValidationError("Los nombres de canales deben ser únicos.")
        channel_names.add(name)
        provider = channel["provider"]
        if not isinstance(provider, str) or provider not in {"telegram", "gotify", "ntfy", "webhook"}:
            raise BackupValidationError(f"El proveedor del canal “{name}” no es compatible.")
        if not isinstance(channel["config"], dict):
            raise BackupValidationError(f"La configuración del canal “{name}” debe ser un objeto.")
        try:
            config = validate_channel_configuration(provider, channel["config"])
        except (ChannelValidationError, TypeError, ValueError, RecursionError) as exc:
            detail = str(exc) if isinstance(exc, ChannelValidationError) else "revisa sus tipos y estructura"
            raise BackupValidationError(f"Configuración no válida para el canal “{name}”: {detail}") from None
        secret_configured = _bool(channel["secret_configured"], f"channels[{index}].secret_configured")
        if secret_configured:
            channels_with_secret.add(key)
        channel_secrets = decoded_bundle.get(key, {})
        try:
            secrets_value = validate_secrets(provider, channel_secrets, config)
        except (ChannelValidationError, TypeError, ValueError, RecursionError):
            raise BackupValidationError(f"Los datos protegidos del canal “{name}” no son válidos.") from None
        if provider == "webhook" and not secrets_value.get("headers"):
            secrets_value = {}
        if encrypted and secret_configured != bool(secrets_value):
            raise BackupValidationError(f"El bloque cifrado del canal “{name}” está incompleto.")
        if not encrypted and secret_configured:
            warnings.append(f"El canal “{name}” tiene credenciales omitidas; quedará deshabilitado hasta configurarlas otra vez.")
        enabled = _bool(channel["enabled"], f"channels[{index}].enabled")
        if provider != "webhook" and enabled and not secret_configured:
            if encrypted:
                raise BackupValidationError(f"El canal “{name}” está habilitado pero no incluye su credencial requerida.")
            enabled = False
        if secret_configured and not encrypted:
            enabled = False
        normalized = {
            "key": key,
            "name": name,
            "provider": provider,
            "config": config,
            "enabled": enabled,
            "timeout_seconds": _integer(channel["timeout_seconds"], f"channels[{index}].timeout_seconds", 1, 60),
            "rate_limit_seconds": _integer(channel["rate_limit_seconds"], f"channels[{index}].rate_limit_seconds", 0, 3600),
            "secrets": secrets_value,
            "secret_configured": secret_configured,
        }
        normalized_channels.append(normalized)
        keys_to_channel[key] = normalized
    if encrypted and set(decoded_bundle) != channels_with_secret:
        raise BackupValidationError("El bloque cifrado no coincide con los canales que tienen credenciales.")

    normalized_rules: list[dict[str, Any]] = []
    rule_names: set[str] = set()
    for index, item in enumerate(raw_rules):
        rule = _object(item, f"rules[{index}]", {
            "name", "description", "enabled", "conditions", "events", "channel_keys", "cooldown_seconds",
            "reminder_interval_seconds", "subject_template", "body_template",
        })
        name = _string(rule["name"], f"rules[{index}].name", 100).strip()
        if name in rule_names:
            raise BackupValidationError("Los nombres de reglas deben ser únicos.")
        rule_names.add(name)
        description = _string(rule["description"], f"rules[{index}].description", 1000, allow_empty=True)
        enabled = _bool(rule["enabled"], f"rules[{index}].enabled")
        if not isinstance(rule["conditions"], dict):
            raise BackupValidationError(f"Las condiciones de la regla “{name}” deben ser un objeto.")
        try:
            conditions = validate_conditions(rule["conditions"])
            events = validate_events(rule["events"])
            subject = validate_template(_string(rule["subject_template"], "subject_template", 500), maximum=500)
            body = validate_template(_string(rule["body_template"], "body_template", 2000), maximum=2000)
        except (RuleValidationError, TypeError, ValueError, RecursionError):
            raise BackupValidationError(f"La regla “{name}” contiene una configuración no válida.") from None
        channel_keys = rule["channel_keys"]
        if not isinstance(channel_keys, list) or len(channel_keys) > BACKUP_MAX_CHANNELS:
            raise BackupValidationError(f"Las asociaciones de canales de la regla “{name}” no son válidas.")
        if any(not isinstance(value, str) or value not in keys_to_channel for value in channel_keys):
            raise BackupValidationError(f"La regla “{name}” referencia un canal que no existe en el backup.")
        if len(set(channel_keys)) != len(channel_keys):
            raise BackupValidationError(f"La regla “{name}” repite un canal asociado.")
        normalized_rules.append({
            "name": name,
            "description": description,
            "enabled": enabled,
            "conditions": conditions,
            "events": events,
            "channel_keys": channel_keys,
            "cooldown_seconds": _integer(rule["cooldown_seconds"], "cooldown_seconds", 0, 31_536_000),
            "reminder_interval_seconds": _integer(rule["reminder_interval_seconds"], "reminder_interval_seconds", 1, 31_536_000, optional=True),
            "subject_template": subject,
            "body_template": body,
        })
    return {
        "settings": normalized_settings,
        "channels": normalized_channels,
        "rules": normalized_rules,
        "warnings": warnings,
        "encrypted": encrypted,
        "exported_at": exported_at,
    }


def preview_summary(normalized: dict[str, Any]) -> dict[str, Any]:
    return {
        "valid": True,
        "schema_version": BACKUP_SCHEMA_VERSION,
        "exported_at": normalized["exported_at"],
        "rules": len(normalized["rules"]),
        "channels": len(normalized["channels"]),
        "enabled_rules": sum(1 for rule in normalized["rules"] if rule["enabled"]),
        "enabled_channels": sum(1 for channel in normalized["channels"] if channel["enabled"]),
        "settings": normalized["settings"],
        "encrypted_secrets": normalized["encrypted"],
        "warnings": normalized["warnings"],
    }


def _capture_current_configuration(db) -> dict[str, Any]:
    """Capture configuration and encrypted secret blobs before a replacement transaction."""
    return {
        "settings": [dict(row) for row in db.execute("SELECT * FROM notification_settings ORDER BY id")],
        "channels": [dict(row) for row in db.execute("SELECT * FROM notification_channels ORDER BY id")],
        "rules": [dict(row) for row in db.execute("SELECT * FROM notification_rules ORDER BY id")],
        "relations": [dict(row) for row in db.execute("SELECT * FROM notification_rule_channels ORDER BY rule_id, channel_id")],
    }


def persist_pre_restore_backup(db, timestamp: str) -> int:
    """Persist a local, config-only recovery point; credentials remain Fernet encrypted."""
    document = export_document(db)
    secret_bundle: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(db.execute("SELECT secrets_ciphertext FROM notification_channels ORDER BY id"), start=1):
        if row["secrets_ciphertext"]:
            secret_bundle[f"channel-{index:04d}"] = decrypt_secrets(row["secrets_ciphertext"])
    secret_ciphertext = encrypt_secrets(secret_bundle) if secret_bundle else None
    cursor = db.execute(
        "INSERT INTO notification_config_backups(created_at, snapshot_json, secrets_ciphertext) VALUES (?, ?, ?)",
        (timestamp, json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")), secret_ciphertext),
    )
    db.execute(
        "DELETE FROM notification_config_backups WHERE id NOT IN (SELECT id FROM notification_config_backups ORDER BY id DESC LIMIT 10)"
    )
    return int(cursor.lastrowid)


def decode_pre_restore_backup(snapshot_json: str, secrets_ciphertext: str | None) -> dict[str, Any]:
    try:
        document = json.loads(snapshot_json, object_pairs_hook=_reject_duplicate_pairs, parse_constant=_reject_constant)
    except (json.JSONDecodeError, ValueError, TypeError):
        raise BackupValidationError("La copia previa guardada no es válida.") from None
    if not isinstance(document, dict) or "secrets_encrypted" in document:
        raise BackupValidationError("La copia previa guardada tiene un formato inválido.")
    secret_bundle = decrypt_secrets(secrets_ciphertext)
    password = secrets.token_urlsafe(32)
    document["secrets_encrypted"] = _encrypt_secrets_bundle(secret_bundle, password)
    return validate_document(document, password)


def previous_backup_summary(db) -> dict[str, Any]:
    row = db.execute(
        "SELECT id, created_at, snapshot_json, secrets_ciphertext FROM notification_config_backups ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return {"available": False}
    try:
        document = json.loads(row["snapshot_json"])
        notifications = document["notifications"]
        channels = notifications["channels"]
        rules = notifications["rules"]
    except (KeyError, TypeError, json.JSONDecodeError):
        raise BackupValidationError("La copia previa guardada no es válida.") from None
    return {
        "available": True,
        "created_at": row["created_at"],
        "channels": len(channels),
        "rules": len(rules),
        "encrypted_secrets": bool(row["secrets_ciphertext"]),
    }


def replace_configuration(db, normalized: dict[str, Any], timestamp: str) -> dict[str, int]:
    # Keep an in-memory pre-restore snapshot (including ciphertext only). SQLite's
    # transaction below is the durable rollback mechanism for exceptions/crashes.
    previous = _capture_current_configuration(db)
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute(
            "UPDATE notification_deliveries SET status='cancelled', updated_at=?, last_error='Notification configuration replaced' WHERE status IN ('pending','in_flight')",
            (timestamp,),
        )
        db.execute("DELETE FROM notification_rules")
        db.execute("DELETE FROM notification_channels")
        db.execute(
            "UPDATE notification_settings SET enabled=?, history_retention_days=?, updated_at=? WHERE id=1",
            (int(normalized["settings"]["enabled"]), normalized["settings"]["history_retention_days"], timestamp),
        )
        channel_ids: dict[str, int] = {}
        for channel in normalized["channels"]:
            ciphertext = encrypt_secrets(channel["secrets"]) if channel["secrets"] else None
            db.execute(
                """INSERT INTO notification_channels
                   (name, provider, config_json, secrets_ciphertext, enabled, timeout_seconds, rate_limit_seconds,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (channel["name"], channel["provider"], json.dumps(channel["config"], ensure_ascii=False, allow_nan=False),
                 ciphertext, int(channel["enabled"]), channel["timeout_seconds"], channel["rate_limit_seconds"], timestamp, timestamp),
            )
            channel_ids[channel["key"]] = db.execute("SELECT last_insert_rowid()").fetchone()[0]

        for rule in normalized["rules"]:
            db.execute(
                """INSERT INTO notification_rules
                   (name, description, enabled, conditions_json, events_json, cooldown_seconds,
                    reminder_interval_seconds, subject_template, body_template, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rule["name"], rule["description"], int(rule["enabled"]),
                 json.dumps(rule["conditions"], ensure_ascii=False, allow_nan=False),
                 json.dumps(rule["events"], ensure_ascii=False, allow_nan=False), rule["cooldown_seconds"],
                 rule["reminder_interval_seconds"], rule["subject_template"], rule["body_template"], timestamp, timestamp),
            )
            rule_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            db.executemany(
                "INSERT INTO notification_rule_channels(rule_id, channel_id) VALUES (?, ?)",
                [(rule_id, channel_ids[key]) for key in rule["channel_keys"]],
            )
        return previous
    except Exception:
        db.rollback()
        # The previous snapshot remains in memory for diagnostics/recovery until
        # rollback completes; never return or log its contents.
        _ = previous
        raise
