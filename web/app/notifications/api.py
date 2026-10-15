from __future__ import annotations

import json
import math
import sqlite3
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..db import connect
from .channel_validation import ChannelValidationError, validate_channel_configuration, validate_secrets
from .crypto import SecretStoreUnavailable, decrypt_secrets, encrypt_secrets
from .providers import send_notification
from .rules import RuleValidationError, rule_matches, validate_conditions, validate_events, validate_template
from .schemas import ChannelWrite, NotificationSettingsWrite, RuleWrite


_request_database_path: ContextVar[Any | None] = ContextVar("notification_database_path", default=None)
_request_offline_after: ContextVar[int] = ContextVar("notification_offline_after", default=900)


async def _bind_request_context(request: Request):
    settings = request.app.state.settings
    path_token = _request_database_path.set(settings.database_path)
    offline_token = _request_offline_after.set(settings.offline_after_seconds)
    try:
        yield
    finally:
        _request_database_path.reset(path_token)
        _request_offline_after.reset(offline_token)


router = APIRouter(
    prefix="/api/v1/notifications", tags=["notifications"],
    dependencies=[Depends(_bind_request_context)],
)


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_or_error(raw: str, label: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(500, f"Stored {label} is invalid") from exc


def _channel_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "provider": row["provider"],
        "config": _json_or_error(row["config_json"], "channel configuration"),
        "enabled": bool(row["enabled"]), "secret_configured": bool(row["secrets_ciphertext"]),
        "has_secret": bool(row["secrets_ciphertext"]), "timeout_seconds": row["timeout_seconds"],
        "rate_limit_seconds": row["rate_limit_seconds"], "last_result": row["last_result"],
        "last_error": row["last_error"], "last_success_at": row["last_success_at"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _channels_for_rule(db: sqlite3.Connection, rule_id: int) -> list[dict[str, Any]]:
    rows = db.execute(
        """SELECT c.id, c.name, c.enabled FROM notification_channels c
           JOIN notification_rule_channels rc ON rc.channel_id = c.id
           WHERE rc.rule_id = ? ORDER BY c.id""",
        (rule_id,),
    ).fetchall()
    return [{"id": row["id"], "name": row["name"], "enabled": bool(row["enabled"])} for row in rows]


def _rule_dict(db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "description": row["description"],
        "enabled": bool(row["enabled"]), "conditions": _json_or_error(row["conditions_json"], "rule conditions"),
        "events": _json_or_error(row["events_json"], "rule events"),
        "channel_ids": [channel["id"] for channel in _channels_for_rule(db, row["id"])],
        "channels": _channels_for_rule(db, row["id"]), "cooldown_seconds": row["cooldown_seconds"],
        "reminder_interval_seconds": row["reminder_interval_seconds"],
        "subject_template": row["subject_template"], "body_template": row["body_template"],
        "match_count": row["match_count"], "last_matched_at": row["last_matched_at"],
        "last_triggered_at": row["last_triggered_at"], "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _validate_rule(value: RuleWrite) -> tuple[dict[str, Any], list[str], str, str]:
    try:
        conditions = validate_conditions(value.conditions)
        events = validate_events(value.events)
        subject = validate_template(value.subject_template, maximum=500)
        body = validate_template(value.body_template, maximum=2000)
    except RuleValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if len(set(value.channel_ids)) != len(value.channel_ids):
        raise HTTPException(422, "channel_ids must not contain duplicates")
    if any(channel_id <= 0 for channel_id in value.channel_ids):
        raise HTTPException(422, "channel_ids must be positive integers")
    return conditions, events, subject, body


def _replace_rule_channels(db: sqlite3.Connection, rule_id: int, channel_ids: list[int]) -> None:
    if channel_ids:
        placeholders = ",".join("?" for _ in channel_ids)
        found = db.execute(
            f"SELECT id FROM notification_channels WHERE id IN ({placeholders})", channel_ids,
        ).fetchall()
        if {row["id"] for row in found} != set(channel_ids):
            raise HTTPException(404, "One or more notification channels do not exist")
    db.execute("DELETE FROM notification_rule_channels WHERE rule_id = ?", (rule_id,))
    db.executemany(
        "INSERT INTO notification_rule_channels(rule_id, channel_id) VALUES (?, ?)",
        [(rule_id, channel_id) for channel_id in channel_ids],
    )


def _prepare_channel(value: ChannelWrite, old: sqlite3.Row | None = None) -> tuple[dict[str, Any], dict[str, Any], str | None]:
    try:
        config = validate_channel_configuration(value.provider, value.config)
        supplied = validate_secrets(value.provider, value.secrets, config)
        previous = {}
        if old is not None and old["provider"] == value.provider and value.secrets is None:
            previous = decrypt_secrets(old["secrets_ciphertext"])
        elif old is not None and old["provider"] == value.provider and value.secrets is not None:
            previous = decrypt_secrets(old["secrets_ciphertext"])
        merged = {**previous, **supplied}
        if value.provider == "webhook":
            # Keep only values for header names still explicitly declared in the public config.
            allowed = {name.casefold() for name in config.get("header_names", [])}
            old_headers = previous.get("headers", {})
            new_headers = supplied.get("headers", {})
            merged["headers"] = {
                **{k: v for k, v in old_headers.items() if k.casefold() in allowed},
                **new_headers,
            }
            if not merged["headers"]:
                merged.pop("headers", None)
        ciphertext = encrypt_secrets(merged) if merged else None
        return config, merged, ciphertext
    except (ChannelValidationError, SecretStoreUnavailable) as exc:
        status_code = 503 if isinstance(exc, SecretStoreUnavailable) else 422
        raise HTTPException(status_code, str(exc)) from exc


def _ensure_channel_secret(provider: str, secrets: dict[str, Any], *, enabled: bool) -> None:
    configured = (
        bool(secrets.get("bot_token")) if provider == "telegram" else
        bool(secrets.get("app_token")) if provider == "gotify" else
        bool(secrets.get("access_token") or (secrets.get("username") and secrets.get("password"))) if provider == "ntfy" else
        True if provider == "webhook" else False
    )
    if enabled and not configured:
        raise HTTPException(422, "Configure the provider secret before enabling this channel")


@router.get("/settings")
def get_settings():
    with connect(_database_path()) as db:
        row = db.execute("SELECT enabled, history_retention_days, updated_at FROM notification_settings WHERE id = 1").fetchone()
    return {"enabled": bool(row["enabled"]), "history_retention_days": row["history_retention_days"], "updated_at": row["updated_at"]}


@router.put("/settings")
def put_settings(value: NotificationSettingsWrite):
    updated = now_text()
    with connect(_database_path()) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE notification_settings SET enabled = ?, history_retention_days = ?, updated_at = ? WHERE id = 1",
            (int(value.enabled), value.history_retention_days, updated),
        )
        if not value.enabled:
            db.execute(
                """UPDATE notification_deliveries SET status = 'cancelled', updated_at = ?, last_error = 'Notification engine disabled'
                   WHERE status = 'pending'""",
                (updated,),
            )
    return {"enabled": value.enabled, "history_retention_days": value.history_retention_days, "updated_at": updated}


@router.get("/channels")
def list_channels(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    with connect(_database_path()) as db:
        total = db.execute("SELECT COUNT(*) FROM notification_channels").fetchone()[0]
        rows = db.execute("SELECT * FROM notification_channels ORDER BY id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
    return {"items": [_channel_dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}


@router.post("/channels", status_code=201)
def create_channel(value: ChannelWrite):
    value.name = value.name.strip()
    if not value.name:
        raise HTTPException(422, "Channel name must not be blank")
    config, secrets, ciphertext = _prepare_channel(value)
    _ensure_channel_secret(value.provider, secrets, enabled=value.enabled)
    now = now_text()
    try:
        with connect(_database_path()) as db:
            db.execute(
                """INSERT INTO notification_channels
                   (name, provider, config_json, secrets_ciphertext, enabled, timeout_seconds,
                    rate_limit_seconds, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (value.name, value.provider, json.dumps(config, ensure_ascii=False), ciphertext, int(value.enabled),
                 value.timeout_seconds, value.rate_limit_seconds, now, now),
            )
            row = db.execute("SELECT * FROM notification_channels WHERE id = last_insert_rowid()").fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "A notification channel with this name already exists") from exc
    return _channel_dict(row)


@router.put("/channels/{channel_id}")
def update_channel(channel_id: int, value: ChannelWrite):
    value.name = value.name.strip()
    if not value.name:
        raise HTTPException(422, "Channel name must not be blank")
    with connect(_database_path()) as db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,)).fetchone()
        if old is None:
            raise HTTPException(404, "Notification channel not found")
        config, secrets, ciphertext = _prepare_channel(value, old)
        _ensure_channel_secret(value.provider, secrets, enabled=value.enabled)
        now = now_text()
        try:
            db.execute(
                """UPDATE notification_channels SET name = ?, provider = ?, config_json = ?, secrets_ciphertext = ?,
                   enabled = ?, timeout_seconds = ?, rate_limit_seconds = ?, updated_at = ?, last_result = NULL,
                   last_error = NULL WHERE id = ?""",
                (value.name.strip(), value.provider, json.dumps(config, ensure_ascii=False), ciphertext,
                 int(value.enabled), value.timeout_seconds, value.rate_limit_seconds, now, channel_id),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "A notification channel with this name already exists") from exc
        if not value.enabled:
            db.execute(
                "UPDATE notification_deliveries SET status='cancelled', updated_at=?, last_error='Channel disabled' WHERE channel_id=? AND status='pending'",
                (now, channel_id),
            )
        row = db.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,)).fetchone()
    return _channel_dict(row)


@router.delete("/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: int):
    with connect(_database_path()) as db:
        now = now_text()
        db.execute(
            "UPDATE notification_deliveries SET status='cancelled', updated_at=?, last_error='Channel deleted' WHERE channel_id=? AND status='pending'",
            (now, channel_id),
        )
        cursor = db.execute("DELETE FROM notification_channels WHERE id = ?", (channel_id,))
        if cursor.rowcount == 0:
            raise HTTPException(404, "Notification channel not found")
    return None


@router.post("/channels/{channel_id}/test")
async def test_channel(channel_id: int):
    with connect(_database_path()) as db:
        row = db.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Notification channel not found")
    try:
        secrets = decrypt_secrets(row["secrets_ciphertext"])
    except SecretStoreUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    _ensure_channel_secret(row["provider"], secrets, enabled=True)

    with connect(_database_path()) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Notification channel not found")
        if row is not None and row["last_attempt_at"]:
            last_attempt = datetime.fromisoformat(row["last_attempt_at"].replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - last_attempt).total_seconds()
            remaining = row["rate_limit_seconds"] - elapsed
            if remaining > 0:
                wait_seconds = max(1, math.ceil(remaining))
                raise HTTPException(
                    429, "This notification channel was tested too recently",
                    headers={"Retry-After": str(wait_seconds)},
                )
        if row is not None:
            now = now_text()
            db.execute(
                "UPDATE notification_channels SET last_attempt_at=?, last_result='testing', last_error=NULL WHERE id=?",
                (now, channel_id),
            )
            row = db.execute("SELECT * FROM notification_channels WHERE id = ?", (channel_id,)).fetchone()
    context = {
        "source": "NaftaMon Web", "source_id": "", "server": "test", "backend": "",
        "type": "service", "host": "test-host", "service": "test-service", "state": "WARNING",
        "old_state": "", "hard": True, "acknowledged": False, "downtime": False,
        "timestamp": now_text(), "event_type": "test",
    }
    result = await send_notification(
        row["provider"], _json_or_error(row["config_json"], "channel configuration"), secrets, context,
        subject_template="NaftaMon Web test", body_template="Prueba de notificación de NaftaMon Web",
        timeout_seconds=row["timeout_seconds"], delivery_id=f"test-{channel_id}-{int(datetime.now(timezone.utc).timestamp())}",
    )
    now = now_text()
    with connect(_database_path()) as db:
        db.execute(
            "UPDATE notification_channels SET last_result=?, last_error=?, last_attempt_at=?, last_success_at=COALESCE(?, last_success_at), updated_at=? WHERE id=?",
            ("sent" if result.ok else "failed", result.error, now, now if result.ok else None, now, channel_id),
        )
    payload = {"ok": result.ok, "status_code": result.status_code, "error": result.error,
               "retry_after_seconds": result.retry_after_seconds}
    return payload if result.ok else JSONResponse(payload, status_code=502)


@router.get("/rules")
def list_rules(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    with connect(_database_path()) as db:
        total = db.execute("SELECT COUNT(*) FROM notification_rules").fetchone()[0]
        rows = db.execute("SELECT * FROM notification_rules ORDER BY id LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        items = [_rule_dict(db, row) for row in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/rules", status_code=201)
def create_rule(value: RuleWrite):
    value.name = value.name.strip()
    if not value.name:
        raise HTTPException(422, "Rule name must not be blank")
    conditions, events, subject, body = _validate_rule(value)
    now = now_text()
    try:
        with connect(_database_path()) as db:
            db.execute(
                """INSERT INTO notification_rules
                   (name, description, enabled, conditions_json, events_json, cooldown_seconds,
                    reminder_interval_seconds, subject_template, body_template, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (value.name.strip(), value.description, int(value.enabled), json.dumps(conditions), json.dumps(events),
                 value.cooldown_seconds, value.reminder_interval_seconds, subject, body, now, now),
            )
            rule_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
            _replace_rule_channels(db, rule_id, value.channel_ids)
            row = db.execute("SELECT * FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
            result = _rule_dict(db, row)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "A notification rule with this name already exists") from exc
    return result


@router.put("/rules/{rule_id}")
def update_rule(rule_id: int, value: RuleWrite):
    value.name = value.name.strip()
    if not value.name:
        raise HTTPException(422, "Rule name must not be blank")
    conditions, events, subject, body = _validate_rule(value)
    now = now_text()
    try:
        with connect(_database_path()) as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT id FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
            if old is None:
                raise HTTPException(404, "Notification rule not found")
            db.execute(
                """UPDATE notification_rules SET name=?, description=?, enabled=?, conditions_json=?, events_json=?,
                   cooldown_seconds=?, reminder_interval_seconds=?, subject_template=?, body_template=?, updated_at=?
                   WHERE id=?""",
                (value.name.strip(), value.description, int(value.enabled), json.dumps(conditions), json.dumps(events),
                 value.cooldown_seconds, value.reminder_interval_seconds, subject, body, now, rule_id),
            )
            _replace_rule_channels(db, rule_id, value.channel_ids)
            if not value.enabled:
                db.execute(
                    "UPDATE notification_deliveries SET status='cancelled', updated_at=?, last_error='Rule disabled' WHERE rule_id=? AND status='pending'",
                    (now, rule_id),
                )
            row = db.execute("SELECT * FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
            result = _rule_dict(db, row)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, "A notification rule with this name already exists") from exc
    return result


@router.delete("/rules/{rule_id}", status_code=204)
def delete_rule(rule_id: int):
    with connect(_database_path()) as db:
        now = now_text()
        db.execute(
            "UPDATE notification_deliveries SET status='cancelled', updated_at=?, last_error='Rule deleted' WHERE rule_id=? AND status='pending'",
            (now, rule_id),
        )
        cursor = db.execute("DELETE FROM notification_rules WHERE id = ?", (rule_id,))
        if cursor.rowcount == 0:
            raise HTTPException(404, "Notification rule not found")
    return None


@router.post("/rules/{rule_id}/duplicate", status_code=201)
def duplicate_rule(rule_id: int):
    now = now_text()
    with connect(_database_path()) as db:
        old = db.execute("SELECT * FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
        if old is None:
            raise HTTPException(404, "Notification rule not found")
        name = f"{old['name']} (copy)"[:100]
        suffix = 2
        while db.execute("SELECT 1 FROM notification_rules WHERE name = ?", (name,)).fetchone():
            name = f"{old['name']} (copy {suffix})"[:100]
            suffix += 1
        cursor = db.execute(
            """INSERT INTO notification_rules
               (name, description, enabled, conditions_json, events_json, cooldown_seconds,
                reminder_interval_seconds, subject_template, body_template, created_at, updated_at)
               VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, old["description"], old["conditions_json"], old["events_json"], old["cooldown_seconds"],
             old["reminder_interval_seconds"], old["subject_template"], old["body_template"], now, now),
        )
        new_id = cursor.lastrowid
        channel_ids = [row[0] for row in db.execute("SELECT channel_id FROM notification_rule_channels WHERE rule_id = ?", (rule_id,)).fetchall()]
        _replace_rule_channels(db, new_id, channel_ids)
        return _rule_dict(db, db.execute("SELECT * FROM notification_rules WHERE id = ?", (new_id,)).fetchone())


@router.post("/rules/{rule_id}/test")
def test_rule(rule_id: int):
    with connect(_database_path()) as db:
        rule = db.execute("SELECT * FROM notification_rules WHERE id = ?", (rule_id,)).fetchone()
        if rule is None:
            raise HTTPException(404, "Notification rule not found")
        conditions = validate_conditions(_json_or_error(rule["conditions_json"], "rule conditions"))
        alerts = db.execute(
            """SELECT a.*, s.source_name FROM active_alerts a JOIN sources s ON s.id = a.source_id
               JOIN monitored_servers ms ON ms.source_id = a.source_id AND ms.name = a.server
               WHERE ms.status = 'ok' AND ms.removed = 0 AND ms.status_revision = s.snapshot_revision
                 AND (julianday('now') - julianday(s.last_received_at)) * 86400 <= ?
               ORDER BY a.server, a.host, a.service LIMIT 500"""
            , (_request_offline_after.get(),)).fetchall()
    matches = []
    for alert in alerts:
        context = {
            "source": alert["source_name"], "source_id": alert["source_id"], "server": alert["server"],
            "backend": alert["backend"], "type": alert["type"], "host": alert["host"],
            "service": alert["service"], "state": alert["state"], "hard": bool(alert["hard"]),
            "acknowledged": bool(alert["acknowledged"]), "downtime": bool(alert["downtime"]),
        }
        if alert["status_information_available"]:
            context["status_information"] = alert["status_information"]
        if rule_matches(context, conditions):
            matches.append({key: context[key] for key in ("source_id", "server", "backend", "type", "host", "service", "state")})
    return {"dry_run": True, "sent": False, "total_examined": len(alerts), "match_count": len(matches), "matches": matches}


@router.get("/history")
def notification_history(
    rule_id: int | None = None,
    channel_id: int | None = None,
    result: str | None = Query(default=None, pattern="^(pending|in_flight|sent|failed|cancelled)$"),
    from_time: str | None = Query(default=None, alias="from", max_length=64),
    to_time: str | None = Query(default=None, alias="to", max_length=64),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    conditions, params = [], []
    if rule_id is not None:
        conditions.append("d.rule_id = ?")
        params.append(rule_id)
    if channel_id is not None:
        conditions.append("d.channel_id = ?")
        params.append(channel_id)
    if result is not None:
        conditions.append("d.status = ?")
        params.append(result)
    if from_time:
        conditions.append("e.occurred_at >= ?")
        params.append(from_time)
    if to_time:
        conditions.append("e.occurred_at <= ?")
        params.append(to_time)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    base = " FROM notification_deliveries d JOIN notification_events e ON e.id=d.event_id"
    with connect(_database_path()) as db:
        total = db.execute("SELECT COUNT(*)" + base + where, params).fetchone()[0]
        rows = db.execute(
            """SELECT d.id, d.rule_id, d.rule_name, d.channel_id, d.channel_name, d.provider, d.status,
               d.attempt_count, d.last_error, d.created_at, e.event_type, e.source_id, e.server, e.backend,
               e.type, e.host, e.service, e.state, e.old_state, e.occurred_at,
               (SELECT MAX(a.finished_at) FROM notification_attempts a WHERE a.delivery_id=d.id) AS last_attempt_at
               """ + base + where + " ORDER BY d.created_at DESC, d.id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        items = [dict(row) for row in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _database_path():
    # Context variables keep concurrent requests and multiple TestClient app
    # instances isolated even though FastAPI routes are module-level.
    path = _request_database_path.get()
    if path is None:
        raise RuntimeError("Notification API router is not bound to an application database")
    return path
