from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from .db import connect
from .models import Alert, OkStatus, Snapshot
from .notifications.events import record_transition


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


def _payload_is_older(incoming: str, latest: str) -> bool:
    incoming_at = datetime.fromisoformat(incoming.replace("Z", "+00:00"))
    latest_at = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    return incoming_at < latest_at


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
            episode_id = uuid4().hex
            revision = 0
            db.execute(
                """INSERT INTO active_alerts
                   (source_id, server, backend, host, service, type, state, hard,
                    acknowledged, downtime, last_check, last_change, first_seen_at, last_seen_at,
                    episode_id, notification_revision, status_information)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id, alert.server, alert.backend, alert.host, alert.service, alert.type,
                    alert.state, int(alert.hard), int(alert.acknowledged), int(alert.downtime),
                    alert.last_check, alert.last_change, received_at, received_at, episode_id, revision,
                    alert.status_information or "",
                ),
            )
            db.execute(
                """INSERT INTO alert_history
                   (source_id, server, backend, host, service, type, event, old_value, new_value, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'opened', NULL, ?, ?)""",
                (source_id, *key, value, received_at),
            )
            record_transition(
                db, source_id=source_id, alert=alert.model_dump(), episode_id=episode_id,
                revision=revision, event_type="opened", old_state=None, occurred_at=received_at,
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
            revision = previous["notification_revision"] + 1
            db.execute(
                """INSERT INTO alert_history
                   (source_id, server, backend, host, service, type, event, old_value, new_value, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'state_changed', ?, ?, ?)""",
                (source_id, *key, previous["state"], alert.state, received_at),
            )
            record_transition(
                db, source_id=source_id, alert=alert.model_dump(), episode_id=previous["episode_id"],
                revision=revision, event_type="state_changed", old_state=previous["state"],
                occurred_at=received_at,
            )
        else:
            revision = previous["notification_revision"]
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
               last_check = ?, last_change = ?, last_seen_at = ?, notification_revision = ?,
               status_information = COALESCE(?, status_information) WHERE id = ?""",
            (
                alert.state, int(alert.hard), int(alert.acknowledged), int(alert.downtime),
                alert.last_check, alert.last_change, received_at, revision,
                alert.status_information if "status_information" in alert.model_fields_set else None,
                previous["id"],
            ),
        )

    for key, previous in existing.items():
        if key not in current:
            revision = previous["notification_revision"] + 1
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
            recovered_alert = {
                "server": previous["server"], "backend": previous["backend"],
                "host": previous["host"], "service": previous["service"],
                "type": previous["type"], "state": previous["state"],
                "hard": bool(previous["hard"]), "acknowledged": bool(previous["acknowledged"]),
                "downtime": bool(previous["downtime"]),
            }
            record_transition(
                db, source_id=source_id, alert=recovered_alert, episode_id=previous["episode_id"],
                revision=revision, event_type="recovered", old_state=previous["state"],
                occurred_at=received_at,
            )
            db.execute("DELETE FROM active_alerts WHERE id = ?", (previous["id"],))


def reconcile_ok_statuses(
    db: sqlite3.Connection,
    source_id: int,
    server: str,
    incoming: list[OkStatus],
    received_at: str,
    snapshot_timestamp: str,
) -> None:
    existing_rows = db.execute(
        "SELECT * FROM monitored_statuses WHERE source_id = ? AND server = ?",
        (source_id, server),
    ).fetchall()
    existing = {(r["server"], r["backend"], r["host"], r["service"], r["type"]): r for r in existing_rows}
    current = {
        (status.server, status.backend, status.host, status.service, status.type): status
        for status in incoming
    }

    for key, status in current.items():
        previous = existing.get(key)
        if previous is None:
            db.execute(
                """INSERT INTO monitored_statuses
                   (source_id, server, backend, host, service, type, state, status_information,
                    hard, acknowledged, downtime, last_check, last_change, first_seen_at,
                    last_seen_at, snapshot_timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id, status.server, status.backend, status.host, status.service,
                    status.type, status.state, status.status_information or "", int(status.hard),
                    int(status.acknowledged), int(status.downtime), status.last_check,
                    status.last_change, received_at, received_at, snapshot_timestamp,
                ),
            )
            continue

        db.execute(
            """UPDATE monitored_statuses SET state = ?, status_information = COALESCE(?, status_information),
               hard = ?, acknowledged = ?, downtime = ?, last_check = ?, last_change = ?,
               last_seen_at = ?, snapshot_timestamp = ? WHERE id = ?""",
            (
                status.state,
                status.status_information if "status_information" in status.model_fields_set else None,
                int(status.hard), int(status.acknowledged),
                int(status.downtime), status.last_check, status.last_change, received_at,
                snapshot_timestamp, previous["id"],
            ),
        )

    for key, previous in existing.items():
        if key not in current:
            db.execute("DELETE FROM monitored_statuses WHERE id = ?", (previous["id"],))


def accept_snapshot(
    database_path,
    snapshot: Snapshot,
    peer_ip: str,
    retention_days: int,
) -> dict[str, Any]:
    source_data = snapshot.model_dump(mode="json", exclude_unset=True)
    hash_data = dict(source_data)
    hash_data.pop("timestamp", None)
    canonical = json.dumps(hash_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    payload_json = json.dumps(source_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    with connect(database_path) as db:
        # Reserve SQLite's single writer before reading the revision. This makes
        # concurrent snapshots from the same source serialize monotonically and
        # also prevents two first-time requests from racing the unique source key.
        db.execute("BEGIN IMMEDIATE")
        received_at = iso_utc()
        row = db.execute(
            "SELECT id, snapshot_revision, payload_timestamp FROM sources WHERE source_name = ? AND peer_ip = ?",
            (snapshot.source, peer_ip),
        ).fetchone()
        if row is None:
            cursor = db.execute(
                """INSERT INTO sources
                   (source_name, peer_ip, first_received_at, last_received_at, payload_timestamp, complete, snapshot_revision)
                   VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (snapshot.source, peer_ip, received_at, received_at, snapshot.timestamp, int(snapshot.complete)),
            )
            source_id = cursor.lastrowid
            source_revision = 1
        else:
            source_id = row["id"]
            if _payload_is_older(snapshot.timestamp, row["payload_timestamp"]):
                # A delayed webhook must not roll back the latest snapshot or any
                # per-server state. Receipt time still reflects observed traffic.
                db.execute("UPDATE sources SET last_received_at = ? WHERE id = ?", (received_at, source_id))
                return {
                    "kind": "snapshot", "source_id": source_id, "source": snapshot.source,
                    "received_at": received_at, "complete": snapshot.complete,
                    "content_hash": content_hash, "stale_snapshot": True,
                }
            source_revision = row["snapshot_revision"] + 1
            db.execute(
                """UPDATE sources SET last_received_at = ?, payload_timestamp = ?, complete = ?,
                   snapshot_revision = ? WHERE id = ?""",
                (received_at, snapshot.timestamp, int(snapshot.complete), source_revision, source_id),
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
        statuses_by_server: dict[str, list[OkStatus]] = {}
        for status in snapshot.ok_statuses or []:
            statuses_by_server.setdefault(status.server, []).append(status)

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
                       (source_id, name, status, status_received_at, last_ok_at, removed, status_revision)
                       VALUES (?, ?, ?, ?, ?, 0, ?)""",
                    (source_id, server.server, server.status, received_at, last_ok, source_revision),
                )
            else:
                db.execute(
                    """UPDATE monitored_servers SET status = ?, status_received_at = ?,
                       last_ok_at = COALESCE(?, last_ok_at), removed = 0, status_revision = ? WHERE id = ?""",
                    (server.status, received_at, last_ok, source_revision, existing["id"]),
                )
            if server.status == "ok":
                reconcile_server(db, source_id, server.server, alerts_by_server.get(server.server, []), received_at)
                if "ok_statuses" in snapshot.model_fields_set:
                    reconcile_ok_statuses(
                        db, source_id, server.server, statuses_by_server.get(server.server, []),
                        received_at, snapshot.timestamp,
                    )

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
        "stale_snapshot": False,
    }
