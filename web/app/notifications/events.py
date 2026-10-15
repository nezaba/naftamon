from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5

from .rules import rule_matches, validate_conditions, validate_events


def utc_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def stable_event_id(
    source_id: int,
    server: str,
    backend: str,
    alert_type: str,
    host: str,
    service: str,
    episode_id: str,
    revision: int,
    event_type: str,
) -> str:
    identity = "\0".join(map(str, (source_id, server, backend, alert_type, host, service, episode_id, revision, event_type)))
    return str(uuid5(NAMESPACE_URL, f"naftamon-notification:{identity}"))


def record_transition(
    db: sqlite3.Connection,
    *,
    source_id: int,
    alert: dict,
    episode_id: str,
    revision: int,
    event_type: str,
    old_state: str | None,
    occurred_at: str,
    only_rule_id: int | None = None,
) -> str:
    source = db.execute("SELECT source_name FROM sources WHERE id = ?", (source_id,)).fetchone()
    context = {
        "source": source["source_name"] if source else "",
        "source_id": source_id,
        "server": alert["server"],
        "backend": alert["backend"],
        "type": alert["type"],
        "host": alert["host"],
        "service": alert["service"],
        "state": alert["state"],
        "old_state": old_state,
        "hard": bool(alert["hard"]),
        "acknowledged": bool(alert["acknowledged"]),
        "downtime": bool(alert["downtime"]),
        "timestamp": occurred_at,
        "event_type": event_type,
    }
    event_id = stable_event_id(
        source_id, alert["server"], alert["backend"], alert["type"], alert["host"],
        alert["service"], episode_id, revision, event_type,
    )
    inserted = db.execute(
        """INSERT INTO notification_events
           (id, source_id, server, backend, type, host, service, episode_id, revision,
            event_type, state, old_state, occurred_at, payload_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO NOTHING""",
        (
            event_id, source_id, alert["server"], alert["backend"], alert["type"], alert["host"],
            alert["service"], episode_id, revision, event_type, alert["state"], old_state,
            occurred_at, json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ),
    )
    if inserted.rowcount == 0:
        return event_id

    settings = db.execute("SELECT enabled FROM notification_settings WHERE id = 1").fetchone()
    if not settings or not settings["enabled"]:
        return event_id

    if only_rule_id is None:
        rules = db.execute("SELECT * FROM notification_rules WHERE enabled = 1 ORDER BY id").fetchall()
    else:
        rules = db.execute(
            "SELECT * FROM notification_rules WHERE enabled = 1 AND id = ?",
            (only_rule_id,),
        ).fetchall()
    for rule in rules:
        try:
            rule_events = validate_events(json.loads(rule["events_json"]))
            conditions = validate_conditions(json.loads(rule["conditions_json"]))
        except (ValueError, json.JSONDecodeError):
            # Invalid legacy data is inert; the worker never evaluates it as code.
            continue
        if event_type not in rule_events or not rule_matches(context, conditions):
            continue

        channels = db.execute(
            """SELECT c.* FROM notification_channels c
               JOIN notification_rule_channels rc ON rc.channel_id = c.id
               WHERE rc.rule_id = ? AND c.enabled = 1 ORDER BY c.id""",
            (rule["id"],),
        ).fetchall()
        if not channels:
            continue

        suppression = db.execute(
            """SELECT last_sent_at FROM notification_suppression_state
               WHERE rule_id = ? AND source_id = ? AND server = ? AND backend = ? AND type = ?
                 AND host = ? AND service = ? AND episode_id = ?""",
            (rule["id"], source_id, alert["server"], alert["backend"], alert["type"], alert["host"], alert["service"], episode_id),
        ).fetchone()
        if suppression and suppression["last_sent_at"] and rule["cooldown_seconds"]:
            try:
                last_sent = datetime.fromisoformat(suppression["last_sent_at"].replace("Z", "+00:00"))
                event_time = datetime.fromisoformat(occurred_at.replace("Z", "+00:00"))
                if (event_time - last_sent).total_seconds() < rule["cooldown_seconds"]:
                    continue
            except ValueError:
                pass

        db.execute(
            "UPDATE notification_rules SET match_count = match_count + 1, last_matched_at = ? WHERE id = ?",
            (occurred_at, rule["id"]),
        )
        for channel in channels:
            db.execute(
                """INSERT INTO notification_deliveries
                   (event_id, rule_id, rule_name, channel_id, channel_name, provider, status,
                    attempt_count, max_attempts, next_attempt_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, 5, ?, ?, ?)
                   ON CONFLICT(event_id, rule_id, channel_id) DO NOTHING""",
                (event_id, rule["id"], rule["name"], channel["id"], channel["name"], channel["provider"], occurred_at, occurred_at, occurred_at),
            )
        db.execute(
            """INSERT INTO notification_suppression_state
               (rule_id, source_id, server, backend, type, host, service, episode_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(rule_id, source_id, server, backend, type, host, service, episode_id) DO NOTHING""",
            (rule["id"], source_id, alert["server"], alert["backend"], alert["type"], alert["host"], alert["service"], episode_id),
        )
        db.execute("UPDATE notification_rules SET last_triggered_at = ? WHERE id = ?", (occurred_at, rule["id"]))
    return event_id
