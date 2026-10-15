from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from typing import Any

from .db import connect
from .models import Alert, Snapshot


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    return (value or utc_now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def alert_key(alert: Alert) -> tuple[str, str, str, str, str]:
    return (alert.server, alert.backend, alert.host, alert.service, alert.type)


def alert_state(alert: Alert) -> str:
    return json.dumps(
        {
            "state": alert.state,
            "hard": alert.hard,
            "acknowledged": alert.acknowledged,
            "downtime": alert.downtime,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def reconcile_server(
    db: sqlite3.Connection,
    source_id: int,
    server: str,
    incoming: list[Alert],
    received_at: str,
) -> None:
    existing_rows = db.execute(
        "SELECT * FROM active_alerts WHERE source_id = ? AND server = ?",
        (source_id, server),
    ).fetchall()
    existing = {(r["server"], r["backend"], r["host"], r["service"], r["type"]): r for r in existing_rows}
    current = {alert_key(alert): alert for alert in incoming}

    for key, alert in current.items():
        previous = existing.get(key)
        value = alert_state(alert)
        if previous is None:
            db.execute(
                """INSERT INTO active_alerts
                   (source_id, server, backend, host, service, type, state, hard,
                    acknowledged, downtime, last_check, last_change, first_seen_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id, alert.server, alert.backend, alert.host, alert.service, alert.type,
                    alert.state, int(alert.hard), int(alert.acknowledged), int(alert.downtime),
                    alert.last_check, alert.last_change, received_at, received_at,
                ),
            )
            db.execute(
                """INSERT INTO alert_history
                   (source_id, server, backend, host, service, type, event, old_value, new_value, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'opened', NULL, ?, ?)""",
                (source_id, *key, value, received_at),
            )
            continue

        previous_value = json.dumps(
            {
                "state": previous["state"], "hard": bool(previous["hard"]),
                "acknowledged": bool(previous["acknowledged"]), "downtime": bool(previous["downtime"]),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if previous["state"] != alert.state:
            db.execute(
                """INSERT INTO alert_history
                   (source_id, server, backend, host, service, type, event, old_value, new_value, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'state_changed', ?, ?, ?)""",
                (source_id, *key, previous["state"], alert.state, received_at),
            )
        previous_flags = (bool(previous["hard"]), bool(previous["acknowledged"]), bool(previous["downtime"]))
        current_flags = (alert.hard, alert.acknowledged, alert.downtime)
        if previous_flags != current_flags:
            db.execute(
                """INSERT INTO alert_history
                   (source_id, server, backend, host, service, type, event, old_value, new_value, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'attributes_changed', ?, ?, ?)""",
                (source_id, *key, previous_value, value, received_at),
            )
        db.execute(
            """UPDATE active_alerts SET state = ?, hard = ?, acknowledged = ?, downtime = ?,
               last_check = ?, last_change = ?, last_seen_at = ? WHERE id = ?""",
            (
                alert.state, int(alert.hard), int(alert.acknowledged), int(alert.downtime),
                alert.last_check, alert.last_change, received_at, previous["id"],
            ),
        )

    for key, previous in existing.items():
        if key not in current:
            old_value = json.dumps(
                {
                    "state": previous["state"], "hard": bool(previous["hard"]),
                    "acknowledged": bool(previous["acknowledged"]), "downtime": bool(previous["downtime"]),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            db.execute(
                """INSERT INTO alert_history
                   (source_id, server, backend, host, service, type, event, old_value, new_value, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'recovered', ?, NULL, ?)""",
                (source_id, *key, old_value, received_at),
            )
            db.execute("DELETE FROM active_alerts WHERE id = ?", (previous["id"],))


def accept_snapshot(
    database_path,
    snapshot: Snapshot,
    peer_ip: str,
    retention_days: int,
) -> dict[str, Any]:
    received_at = iso_utc()
    source_data = snapshot.model_dump(mode="json")
    hash_data = dict(source_data)
    hash_data.pop("timestamp", None)
    canonical = json.dumps(hash_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload_json = json.dumps(source_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    with connect(database_path) as db:
        row = db.execute(
            "SELECT id FROM sources WHERE source_name = ? AND peer_ip = ?",
            (snapshot.source, peer_ip),
        ).fetchone()
        if row is None:
            cursor = db.execute(
                """INSERT INTO sources
                   (source_name, peer_ip, first_received_at, last_received_at, payload_timestamp, complete)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (snapshot.source, peer_ip, received_at, received_at, snapshot.timestamp, int(snapshot.complete)),
            )
            source_id = cursor.lastrowid
        else:
            source_id = row["id"]
            db.execute(
                """UPDATE sources SET last_received_at = ?, payload_timestamp = ?, complete = ?
                   WHERE id = ?""",
                (received_at, snapshot.timestamp, int(snapshot.complete), source_id),
            )

        db.execute(
            """INSERT INTO latest_snapshots
               (source_id, payload_json, content_hash, payload_timestamp, received_at, complete)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id) DO UPDATE SET payload_json = excluded.payload_json,
                 content_hash = excluded.content_hash, payload_timestamp = excluded.payload_timestamp,
                 received_at = excluded.received_at, complete = excluded.complete""",
            (source_id, payload_json, content_hash, snapshot.timestamp, received_at, int(snapshot.complete)),
        )

        seen_servers = set()
        alerts_by_server: dict[str, list[Alert]] = {}
        for alert in snapshot.alerts:
            alerts_by_server.setdefault(alert.server, []).append(alert)

        for server in snapshot.servers:
            seen_servers.add(server.server)
            existing = db.execute(
                "SELECT id FROM monitored_servers WHERE source_id = ? AND name = ?",
                (source_id, server.server),
            ).fetchone()
            last_ok = received_at if server.status == "ok" else None
            if existing is None:
                db.execute(
                    """INSERT INTO monitored_servers
                       (source_id, name, status, status_received_at, last_ok_at, removed)
                       VALUES (?, ?, ?, ?, ?, 0)""",
                    (source_id, server.server, server.status, received_at, last_ok),
                )
            else:
                db.execute(
                    """UPDATE monitored_servers SET status = ?, status_received_at = ?,
                       last_ok_at = COALESCE(?, last_ok_at), removed = 0 WHERE id = ?""",
                    (server.status, received_at, last_ok, existing["id"]),
                )
            if server.status == "ok":
                reconcile_server(db, source_id, server.server, alerts_by_server.get(server.server, []), received_at)

        if snapshot.complete:
            known = db.execute("SELECT id, name FROM monitored_servers WHERE source_id = ? AND removed = 0", (source_id,)).fetchall()
            for server in known:
                if server["name"] not in seen_servers:
                    db.execute(
                        "UPDATE monitored_servers SET status = 'removed', status_received_at = ?, removed = 1 WHERE id = ?",
                        (received_at, server["id"]),
                    )

        if retention_days > 0:
            cutoff = iso_utc(utc_now() - timedelta(days=retention_days))
            db.execute("DELETE FROM alert_history WHERE observed_at < ?", (cutoff,))

    return {
        "kind": "snapshot",
        "source_id": source_id,
        "source": snapshot.source,
        "received_at": received_at,
        "complete": snapshot.complete,
        "content_hash": content_hash,
    }
