from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken


class SecretStoreUnavailable(RuntimeError):
    pass


def _fernet() -> Fernet:
    key_path = Path(os.environ.get("NOTIFICATION_KEY_FILE", "/run/naftamon-secrets/notification.key"))
    try:
        key = key_path.read_bytes().strip()
    except OSError as exc:
        raise SecretStoreUnavailable("Notification encryption key is unavailable") from exc
    try:
        return Fernet(key)
    except (TypeError, ValueError) as exc:
        raise SecretStoreUnavailable("Notification encryption key is invalid") from exc


def encrypt_secrets(value: dict[str, Any]) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return _fernet().encrypt(payload).decode("ascii")
    except (TypeError, ValueError) as exc:
        raise SecretStoreUnavailable("Notification secrets could not be encrypted") from exc


def decrypt_secrets(ciphertext: str | None) -> dict[str, Any]:
    if not ciphertext:
        return {}
    try:
        value = json.loads(_fernet().decrypt(ciphertext.encode("ascii")))
    except (InvalidToken, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise SecretStoreUnavailable("Notification secret could not be decrypted") from exc
    if not isinstance(value, dict):
        raise SecretStoreUnavailable("Notification secret payload is invalid")
    return value
