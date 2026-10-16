from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import json
import sqlite3
from typing import Any, BinaryIO, Iterable
from uuid import uuid4

from .db import _fold_search, connect
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


def _search_fields(item: Alert | OkStatus, status_information: str | None = None) -> tuple[str, str, str]:
    info = item.status_information if status_information is None else status_information
    combined = " ".join((item.server, item.backend, item.host, item.service, item.type, item.state, info))
    return _fold_search(combined), _fold_search(item.host), _fold_search(item.service)


def _notification_alert(alert: Alert, previous: sqlite3.Row | None = None) -> dict[str, Any]:
    """Keep absent legacy fields absent while retaining the last known value."""
    value = alert.model_dump()
    if "status_information" not in alert.model_fields_set:
        value.pop("status_information", None)
        if previous is not None and previous["status_information_available"]:
            value["status_information"] = previous["status_information"]
    return value


def _payload_is_older(incoming: str, latest: str) -> bool:
    incoming_at = datetime.fromisoformat(incoming.replace("Z", "+00:00"))
    latest_at = datetime.fromisoformat(latest.replace("Z", "+00:00"))
    return incoming_at < latest_at


def _snapshot_content_hash(
    snapshot: Snapshot,
    streamed_ok_statuses: BinaryIO | None = None,
    streamed_ok_statuses_present: bool = False,
) -> str:
    """Hash the canonical snapshot without its changing root timestamp, streaming arrays."""
    fields: dict[str, Any] = {
        "alerts": snapshot.alerts,
        "complete": snapshot.complete,
        "servers": snapshot.servers,
        "source": snapshot.source,
    }
    if "ok_statuses" in snapshot.model_fields_set or streamed_ok_statuses_present:
        fields["ok_statuses"] = snapshot.ok_statuses
    digest = hashlib.sha256()
    digest.update(b"{")
    for field_index, name in enumerate(sorted(fields)):
        if field_index:
            digest.update(b",")
        digest.update(json.dumps(name, ensure_ascii=False).encode("utf-8"))
        digest.update(b":")
        value = fields[name]
        if name in ("alerts", "ok_statuses", "servers"):
            digest.update(b"[")
            if name == "ok_statuses" and streamed_ok_statuses is not None:
                streamed_ok_statuses.seek(0)
                for item_index, line in enumerate(streamed_ok_statuses):
                    if item_index:
                        digest.update(b",")
                    digest.update(line.rstrip(b"\r\n"))
                streamed_ok_statuses.seek(0)
            else:
                for item_index, item in enumerate(value or []):
                    if item_index:
                        digest.update(b",")
                    digest.update(json.dumps(
                        item.model_dump(mode="json", exclude_unset=True),
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode("utf-8"))
            digest.update(b"]")
        else:
            digest.update(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    digest.update(b"}")
    return digest.hexdigest()


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
            search_text, host_search, service_search = _search_fields(alert)
            db.execute(
                """INSERT INTO active_alerts
                   (source_id, server, backend, host, service, type, state, hard,
                    acknowledged, downtime, last_check, last_change, first_seen_at, last_seen_at,
                    episode_id, notification_revision, status_information, status_information_available,
                    search_text, host_search, service_search)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id, alert.server, alert.backend, alert.host, alert.service, alert.type,
                    alert.state, int(alert.hard), int(alert.acknowledged), int(alert.downtime),
                    alert.last_check, alert.last_change, received_at, received_at, episode_id, revision,
                    alert.status_information,
                    int("status_information" in alert.model_fields_set),
                    search_text, host_search, service_search,
                ),
            )
            db.execute(
                """INSERT INTO alert_history
                   (source_id, server, backend, host, service, type, event, old_value, new_value, observed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'opened', NULL, ?, ?)""",
                (source_id, *key, value, received_at),
            )
            record_transition(
                db, source_id=source_id, alert=_notification_alert(alert), episode_id=episode_id,
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
                db, source_id=source_id, alert=_notification_alert(alert, previous), episode_id=previous["episode_id"],
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
        search_info = alert.status_information if "status_information" in alert.model_fields_set else previous["status_information"]
        search_text, host_search, service_search = _search_fields(alert, search_info)
        db.execute(
            """UPDATE active_alerts SET state = ?, hard = ?, acknowledged = ?, downtime = ?,
               last_check = ?, last_change = ?, last_seen_at = ?, notification_revision = ?,
               status_information = COALESCE(?, status_information),
               status_information_available = COALESCE(?, status_information_available),
               search_text = ?, host_search = ?, service_search = ? WHERE id = ?""",
            (
                alert.state, int(alert.hard), int(alert.acknowledged), int(alert.downtime),
                alert.last_check, alert.last_change, received_at, revision,
                alert.status_information if "status_information" in alert.model_fields_set else None,
                1 if "status_information" in alert.model_fields_set else None,
                search_text, host_search, service_search,
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
            if previous["status_information_available"]:
                recovered_alert["status_information"] = previous["status_information"]
            record_transition(
                db, source_id=source_id, alert=recovered_alert, episode_id=previous["episode_id"],
                revision=revision, event_type="recovered", old_state=previous["state"],
                occurred_at=received_at,
            )
            db.execute("DELETE FROM active_alerts WHERE id = ?", (previous["id"],))


def _stage_ok_statuses(
    db: sqlite3.Connection,
    incoming: Iterable[OkStatus | dict[str, Any]],
) -> None:
    # Stage a complete snapshot once, then let SQLite reconcile per server with
    # set-wise UPDATE/INSERT/DELETE. The iterable can read JSONL from a spooled
    # file, so the large ok_statuses array is never retained as model objects.
    db.execute("""CREATE TEMP TABLE IF NOT EXISTS incoming_monitored_statuses (
        server TEXT NOT NULL, backend TEXT NOT NULL, host TEXT NOT NULL, service TEXT NOT NULL, type TEXT NOT NULL,
        status_information TEXT NOT NULL, information_available INTEGER NOT NULL,
        search_text TEXT NOT NULL, host_search TEXT NOT NULL, service_search TEXT NOT NULL,
        hard INTEGER NOT NULL, acknowledged INTEGER NOT NULL, downtime INTEGER NOT NULL,
        last_check TEXT NOT NULL, last_change TEXT NOT NULL,
        PRIMARY KEY (server, backend, type, host, service)
    ) WITHOUT ROWID""")
    db.execute("DELETE FROM incoming_monitored_statuses")

    def values(status: OkStatus | dict[str, Any]):
        if isinstance(status, OkStatus):
            get = lambda name, default=None: getattr(status, name, default)
            information_available = "status_information" in status.model_fields_set
            search_text, host_search, service_search = _search_fields(status)
        else:
            get = status.get
            information_available = "status_information" in status
            combined = " ".join((status["server"], status["backend"], status["host"],
                                 status["service"], status["type"], status["state"],
                                 status.get("status_information", "")))
            search_text, host_search, service_search = (
                _fold_search(combined), _fold_search(status["host"]), _fold_search(status["service"]),
            )
        return (
            get("server"), get("backend"), get("host"), get("service"), get("type"),
            get("status_information", ""), int(information_available),
            search_text, host_search, service_search, int(get("hard")),
            int(get("acknowledged")), int(get("downtime")), get("last_check"), get("last_change"),
        )

    db.executemany(
        """INSERT INTO incoming_monitored_statuses
           (server, backend, host, service, type, status_information, information_available,
            search_text, host_search, service_search,
            hard, acknowledged, downtime, last_check, last_change)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (values(status) for status in incoming),
    )


def reconcile_ok_statuses(
    db: sqlite3.Connection,
    source_id: int,
    server: str,
    received_at: str,
    snapshot_timestamp: str,
) -> None:
    # Existing stable rows are left untouched. Effective last-seen time is read
    # from monitored_servers, so polling the same 165k statuses does not rewrite
    # every row or maintain its indexes on each interval.
    db.execute(
        """UPDATE monitored_statuses AS current
           SET status_information = CASE WHEN incoming.information_available = 1
                                         THEN incoming.status_information ELSE current.status_information END,
               search_text = CASE WHEN incoming.information_available = 1 THEN incoming.search_text ELSE current.search_text END,
               hard = incoming.hard, acknowledged = incoming.acknowledged, downtime = incoming.downtime,
               last_check = incoming.last_check, last_change = incoming.last_change
           FROM incoming_monitored_statuses AS incoming
           WHERE current.source_id = ? AND current.server = ?
             AND incoming.server = ?
             AND current.backend = incoming.backend AND current.type = incoming.type
             AND current.host = incoming.host AND current.service = incoming.service
             AND (current.hard != incoming.hard OR current.acknowledged != incoming.acknowledged
                  OR current.downtime != incoming.downtime OR current.last_check != incoming.last_check
                  OR current.last_change != incoming.last_change
                  OR (incoming.information_available = 1
                      AND (current.status_information != incoming.status_information
                           OR current.search_text != incoming.search_text)))""",
        (source_id, server, server),
    )
    db.execute(
        """INSERT INTO monitored_statuses
           (source_id, server, backend, host, service, type, state, status_information,
            search_text, host_search, service_search,
            hard, acknowledged, downtime, last_check, last_change, first_seen_at,
            last_seen_at, snapshot_timestamp)
           SELECT ?, ?, incoming.backend, incoming.host, incoming.service, incoming.type, 'UP',
                  incoming.status_information, incoming.search_text, incoming.host_search, incoming.service_search,
                  incoming.hard, incoming.acknowledged, incoming.downtime,
                  incoming.last_check, incoming.last_change, ?, ?, ?
             FROM incoming_monitored_statuses AS incoming
            WHERE NOT EXISTS (
                SELECT 1 FROM monitored_statuses AS current
                  WHERE current.source_id = ? AND current.server = ? AND incoming.server = ?
                   AND current.backend = incoming.backend AND current.type = incoming.type
                   AND current.host = incoming.host AND current.service = incoming.service
            )""",
        (source_id, server, received_at, received_at, snapshot_timestamp, source_id, server, server),
    )
    db.execute(
        """DELETE FROM monitored_statuses AS current
            WHERE current.source_id = ? AND current.server = ?
             AND NOT EXISTS (
                 SELECT 1 FROM incoming_monitored_statuses AS incoming
                  WHERE incoming.server = ? AND current.backend = incoming.backend AND current.type = incoming.type
                    AND current.host = incoming.host AND current.service = incoming.service
             )""",
        (source_id, server, server),
    )


def accept_snapshot(
    database_path,
    snapshot: Snapshot,
    peer_ip: str,
    retention_days: int,
    raw_json: bytes | None = None,
    streamed_ok_statuses: BinaryIO | None = None,
    streamed_ok_statuses_present: bool = False,
) -> dict[str, Any]:
    content_hash = _snapshot_content_hash(snapshot, streamed_ok_statuses, streamed_ok_statuses_present)
    payload_bytes = raw_json or snapshot.model_dump_json(exclude_unset=True, ensure_ascii=False).encode("utf-8")
    payload_gzip = gzip.compress(payload_bytes, compresslevel=1, mtime=0)

    with connect(database_path) as db:
        # Reserve SQLite's single writer before reading the revision. This makes
        # concurrent snapshots from the same source serialize monotonically and
        # also prevents two first-time requests from racing the unique source key.
        db.execute("BEGIN IMMEDIATE")
        received_at = iso_utc()
        row = db.execute(
            "SELECT id, snapshot_revision, payload_timestamp, deleted_at FROM sources WHERE source_name = ? AND peer_ip = ?",
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
            if row["deleted_at"] is not None:
                # A manually removed source is a tombstone for notification-history
                # foreign keys only. Its next valid snapshot starts a fresh monitor.
                source_revision = 1
                db.execute(
                    """UPDATE sources SET first_received_at = ?, last_received_at = ?,
                       payload_timestamp = ?, complete = ?, snapshot_revision = ?, deleted_at = NULL
                       WHERE id = ?""",
                    (received_at, received_at, snapshot.timestamp, int(snapshot.complete), source_revision, source_id),
                )
            elif _payload_is_older(snapshot.timestamp, row["payload_timestamp"]):
                # A delayed webhook must not roll back the latest snapshot or any
                # per-server state. Receipt time still reflects observed traffic.
                db.execute("UPDATE sources SET last_received_at = ? WHERE id = ?", (received_at, source_id))
                return {
                    "kind": "snapshot", "source_id": source_id, "source": snapshot.source,
                    "received_at": received_at, "complete": snapshot.complete,
                    "content_hash": content_hash, "stale_snapshot": True,
                }
            else:
                source_revision = row["snapshot_revision"] + 1
                db.execute(
                    """UPDATE sources SET last_received_at = ?, payload_timestamp = ?, complete = ?,
                       snapshot_revision = ? WHERE id = ?""",
                    (received_at, snapshot.timestamp, int(snapshot.complete), source_revision, source_id),
                )

        db.execute(
            """INSERT INTO latest_snapshots
               (source_id, payload_json, content_hash, payload_timestamp, received_at, complete,
                payload_gzip, payload_encoding)
               VALUES (?, '{}', ?, ?, ?, ?, ?, 'gzip-json')
               ON CONFLICT(source_id) DO UPDATE SET payload_json = '{}',
                 content_hash = excluded.content_hash, payload_timestamp = excluded.payload_timestamp,
                 received_at = excluded.received_at, complete = excluded.complete,
                 payload_gzip = excluded.payload_gzip, payload_encoding = excluded.payload_encoding""",
            (source_id, content_hash, snapshot.timestamp, received_at, int(snapshot.complete), payload_gzip),
        )
        db.execute("UPDATE app_state SET value = value + 1 WHERE key = 'monitoring_revision'")

        seen_servers = set()
        alerts_by_server: dict[str, list[Alert]] = {}
        for alert in snapshot.alerts:
            alerts_by_server.setdefault(alert.server, []).append(alert)
        statuses_by_server: dict[str, list[OkStatus]] = {}
        has_ok_statuses = streamed_ok_statuses_present or "ok_statuses" in snapshot.model_fields_set
        if has_ok_statuses:
            if streamed_ok_statuses is not None:
                def streamed_items():
                    streamed_ok_statuses.seek(0)
                    for line in streamed_ok_statuses:
                        yield json.loads(line)
                _stage_ok_statuses(db, streamed_items())
            else:
                for status in snapshot.ok_statuses or []:
                    statuses_by_server.setdefault(status.server, []).append(status)
                _stage_ok_statuses(
                    db,
                    (status for values in statuses_by_server.values() for status in values),
                )

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
                if has_ok_statuses:
                    db.execute(
                        "UPDATE monitored_servers SET status_data_revision = ? WHERE source_id = ? AND name = ?",
                        (source_revision, source_id, server.server),
                    )
                    reconcile_ok_statuses(db, source_id, server.server, received_at, snapshot.timestamp)

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
