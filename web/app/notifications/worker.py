from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..db import connect, initialize
from ..settings import Settings
from .crypto import SecretStoreUnavailable, decrypt_secrets
from .events import record_transition
from .providers import DeliveryResult, send_notification
from .rules import rule_matches, validate_conditions, validate_events


LOGGER = logging.getLogger("naftamon.notifications")
DEFAULT_POLL_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 6 * 60 * 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _text(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_text(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _backoff_seconds(attempt: int) -> int:
    return min(MAX_BACKOFF_SECONDS, 2 ** max(0, min(attempt - 1, 15)))


def _event_context(row: sqlite3.Row) -> dict[str, Any]:
    try:
        value = json.loads(row["payload_json"])
    except (KeyError, TypeError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


class NotificationWorker:
    """Single-consumer SQLite outbox worker.

    Claims are committed before network I/O. A lease lets a later process
    recover work after a crash; the lease token prevents an expired worker from
    overwriting a newer attempt. Delivery is at-least-once, so providers that
    accept Idempotency-Key can deduplicate a retry.
    """

    def __init__(
        self,
        database_path: Path,
        *,
        offline_after_seconds: int = 900,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        sender: Callable[..., Any] = send_notification,
    ):
        self.database_path = Path(database_path)
        self.offline_after_seconds = max(1, int(offline_after_seconds))
        self.poll_seconds = max(0.05, float(poll_seconds))
        self.sender = sender
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        initialize(self.database_path)
        while not self._stop.is_set():
            try:
                self.schedule_due_reminders()
                processed = await self.process_one()
            except sqlite3.Error:
                LOGGER.exception("Notification worker database operation failed")
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
                except asyncio.TimeoutError:
                    pass

    async def process_one(self) -> bool:
        claim = self._claim_one()
        if claim is None:
            return False
        started = _now()
        try:
            secrets = decrypt_secrets(claim["secrets_ciphertext"])
            context = _event_context(claim)
            result: DeliveryResult = await self.sender(
                claim["provider"],
                json.loads(claim["config_json"]),
                secrets,
                context,
                subject_template=claim["subject_template"],
                body_template=claim["body_template"],
                timeout_seconds=claim["timeout_seconds"],
                delivery_id=str(claim["id"]),
            )
        except SecretStoreUnavailable:
            # Missing/rotated key files are configuration faults, not a reason
            # to expose ciphertext or secrets in logs. Retrying is useful after
            # the operator restores the mounted key.
            result = DeliveryResult(False, True, "Notification encryption key is unavailable")
        except (ValueError, TypeError, json.JSONDecodeError):
            result = DeliveryResult(False, False, "Stored notification configuration is invalid")
        except Exception:
            # Do not log exception text or tracebacks: third-party HTTP errors
            # can embed complete request URLs, including provider credentials.
            LOGGER.error("Unexpected notification provider failure (delivery %s)", claim["id"])
            result = DeliveryResult(False, True, "Provider connection failed")
        elapsed_ms = max(0, int((_now() - started).total_seconds() * 1000))
        self._finish(claim, result, elapsed_ms)
        return True

    def _claim_one(self) -> dict[str, Any] | None:
        now = _now()
        now_value = _text(now)
        with connect(self.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            # A worker that died mid-request leaves an in_flight row. Requeue
            # within its attempt budget, otherwise retain a terminal failure.
            db.execute(
                """UPDATE notification_deliveries
                   SET status = CASE WHEN attempt_count < max_attempts THEN 'pending' ELSE 'failed' END,
                       lease_until = NULL,
                       next_attempt_at = CASE WHEN attempt_count < max_attempts THEN ? ELSE next_attempt_at END,
                       updated_at = ?,
                       last_error = 'Previous delivery worker lease expired'
                   WHERE status = 'in_flight' AND lease_until IS NOT NULL AND lease_until <= ?""",
                (now_value, now_value, now_value),
            )
            settings = db.execute("SELECT enabled FROM notification_settings WHERE id = 1").fetchone()
            if not settings or not settings["enabled"]:
                db.execute(
                    """UPDATE notification_deliveries SET status='cancelled', lease_until=NULL,
                       updated_at=?, last_error='Notification engine disabled'
                       WHERE status='pending'""",
                    (now_value,),
                )
                return None
            row = db.execute(
                """SELECT d.*, e.payload_json, c.config_json, c.secrets_ciphertext,
                          c.timeout_seconds, c.rate_limit_seconds, c.last_attempt_at,
                          r.subject_template, r.body_template
                   FROM notification_deliveries d
                   JOIN notification_events e ON e.id = d.event_id
                   JOIN notification_channels c ON c.id = d.channel_id
                   JOIN notification_rules r ON r.id = d.rule_id
                   WHERE d.status = 'pending' AND d.next_attempt_at <= ?
                     AND c.enabled = 1 AND r.enabled = 1
                     AND (c.last_attempt_at IS NULL OR
                          julianday(c.last_attempt_at) + c.rate_limit_seconds / 86400.0 <= julianday(?))
                   ORDER BY d.next_attempt_at, d.id LIMIT 1""",
                (now_value, now_value),
            ).fetchone()
            if row is None:
                return None
            lease_until = _text(now + timedelta(seconds=max(60, row["timeout_seconds"] + 30)))
            attempt_number = row["attempt_count"] + 1
            db.execute(
                """UPDATE notification_deliveries
                   SET status = 'in_flight', attempt_count = ?, lease_until = ?, updated_at = ?
                   WHERE id = ? AND status = 'pending'""",
                (attempt_number, lease_until, now_value, row["id"]),
            )
            db.execute(
                "UPDATE notification_channels SET last_attempt_at = ? WHERE id = ?",
                (now_value, row["channel_id"]),
            )
            claim = dict(row)
            claim["attempt_number"] = attempt_number
            claim["attempted_at"] = now_value
            claim["lease_until"] = lease_until
            return claim

    def _finish(self, claim: dict[str, Any], result: DeliveryResult, duration_ms: int) -> None:
        now = _now()
        now_value = _text(now)
        attempt = claim["attempt_number"]
        if result.ok:
            status, outcome, next_attempt, error = "sent", "sent", now_value, None
        elif result.transient and attempt < claim["max_attempts"]:
            delay = result.retry_after_seconds
            if delay is None:
                delay = _backoff_seconds(attempt)
            delay = max(0, min(MAX_BACKOFF_SECONDS, int(delay)))
            status, outcome, next_attempt, error = "pending", "retry", _text(now + timedelta(seconds=delay)), result.error
        else:
            status, outcome, next_attempt, error = "failed", "failed", now_value, result.error

        with connect(self.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                """SELECT d.rule_id, d.channel_id, e.source_id, e.server, e.backend, e.type,
                          e.host, e.service, e.episode_id, e.event_type
                   FROM notification_deliveries d JOIN notification_events e ON e.id=d.event_id
                   WHERE d.id = ? AND d.status = 'in_flight' AND d.lease_until = ?""",
                (claim["id"], claim["lease_until"]),
            ).fetchone()
            if current is None:
                return

            can_retry = status == "pending"
            if can_retry:
                enabled = db.execute(
                    """SELECT s.enabled AS engine_enabled, r.enabled AS rule_enabled, c.enabled AS channel_enabled
                       FROM notification_settings s JOIN notification_rules r ON r.id = ?
                       JOIN notification_channels c ON c.id = ? WHERE s.id = 1""",
                    (current["rule_id"], current["channel_id"]),
                ).fetchone()
                if not enabled or not all(enabled):
                    status, outcome, error = "cancelled", "failed", "Notification delivery disabled before retry"

            db.execute(
                """UPDATE notification_deliveries SET status = ?, next_attempt_at = ?, lease_until = NULL,
                   last_error = ?, updated_at = ? WHERE id = ? AND status = 'in_flight' AND lease_until = ?""",
                (status, next_attempt, error, now_value, claim["id"], claim["lease_until"]),
            )
            db.execute(
                """INSERT INTO notification_attempts
                   (delivery_id, attempt_number, attempted_at, finished_at, outcome, http_status, error, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (claim["id"], attempt, claim["attempted_at"], now_value, outcome,
                 result.status_code, error, duration_ms),
            )
            if current["channel_id"] is not None:
                db.execute(
                    """UPDATE notification_channels SET last_result = ?, last_error = ?,
                       last_success_at = CASE WHEN ? = 1 THEN ? ELSE last_success_at END, updated_at = ?
                       WHERE id = ?""",
                    ("sent" if result.ok else ("retrying" if status == "pending" else status), error,
                     int(result.ok), now_value, now_value, current["channel_id"]),
                )
            if result.ok and current["rule_id"] is not None:
                rule = db.execute(
                    "SELECT reminder_interval_seconds FROM notification_rules WHERE id = ?",
                    (current["rule_id"],),
                ).fetchone()
                if rule:
                    interval = rule["reminder_interval_seconds"]
                    next_reminder = _text(now + timedelta(seconds=interval)) if interval else None
                    if current["event_type"] == "recovered":
                        next_reminder = None
                    db.execute(
                        """UPDATE notification_suppression_state SET last_sent_at = ?,
                           next_reminder_at = ?
                           WHERE rule_id = ? AND source_id = ? AND server = ? AND backend = ? AND type = ?
                             AND host = ? AND service = ? AND episode_id = ?""",
                        (now_value, next_reminder, current["rule_id"], current["source_id"],
                         current["server"], current["backend"], current["type"], current["host"],
                         current["service"], current["episode_id"]),
                    )

    def schedule_due_reminders(self, *, now: datetime | None = None) -> int:
        current_time = now or _now()
        now_value = _text(current_time)
        with connect(self.database_path) as db:
            db.execute("BEGIN IMMEDIATE")
            settings = db.execute(
                "SELECT enabled, history_retention_days FROM notification_settings WHERE id = 1"
            ).fetchone()
            if not settings:
                return 0
            if settings["history_retention_days"] > 0:
                cutoff = _text(current_time - timedelta(days=settings["history_retention_days"]))
                db.execute(
                    """DELETE FROM notification_events
                       WHERE occurred_at < ? AND NOT EXISTS (
                           SELECT 1 FROM notification_deliveries d
                           WHERE d.event_id = notification_events.id AND d.status IN ('pending', 'in_flight')
                       )""",
                    (cutoff,),
                )
            if not settings["enabled"]:
                return 0

            due = db.execute(
                """SELECT s.*, r.enabled AS rule_enabled, r.events_json, r.conditions_json,
                          r.reminder_interval_seconds
                   FROM notification_suppression_state s
                   JOIN notification_rules r ON r.id = s.rule_id
                   WHERE s.next_reminder_at IS NOT NULL AND s.next_reminder_at <= ?
                     AND r.enabled = 1 AND r.reminder_interval_seconds IS NOT NULL
                   ORDER BY s.next_reminder_at LIMIT 200""",
                (now_value,),
            ).fetchall()
            scheduled = 0
            for suppression in due:
                active = db.execute(
                    """SELECT a.*, src.source_name, src.last_received_at, src.snapshot_revision,
                              ms.status AS server_status, ms.status_revision, ms.removed
                       FROM active_alerts a
                       JOIN sources src ON src.id = a.source_id
                       JOIN monitored_servers ms ON ms.source_id = a.source_id AND ms.name = a.server
                       WHERE a.source_id = ? AND a.server = ? AND a.backend = ? AND a.type = ?
                         AND a.host = ? AND a.service = ? AND a.episode_id = ?""",
                    (suppression["source_id"], suppression["server"], suppression["backend"],
                     suppression["type"], suppression["host"], suppression["service"], suppression["episode_id"]),
                ).fetchone()
                if active is None:
                    db.execute(
                        "UPDATE notification_suppression_state SET next_reminder_at = NULL WHERE rule_id=? AND source_id=? AND server=? AND backend=? AND type=? AND host=? AND service=? AND episode_id=?",
                        tuple(suppression[k] for k in ("rule_id", "source_id", "server", "backend", "type", "host", "service", "episode_id")),
                    )
                    continue

                freshness = max(0, (current_time - _parse_text(active["last_received_at"])).total_seconds())
                if (active["server_status"] != "ok" or active["removed"] or
                        active["status_revision"] < active["snapshot_revision"] or
                        freshness > self.offline_after_seconds):
                    later = _text(current_time + timedelta(seconds=suppression["reminder_interval_seconds"]))
                    db.execute(
                        """UPDATE notification_suppression_state SET next_reminder_at = ?
                           WHERE rule_id=? AND source_id=? AND server=? AND backend=? AND type=? AND host=? AND service=? AND episode_id=?""",
                        (later, *tuple(suppression[k] for k in ("rule_id", "source_id", "server", "backend", "type", "host", "service", "episode_id"))),
                    )
                    continue

                try:
                    events = validate_events(json.loads(suppression["events_json"]))
                    conditions = validate_conditions(json.loads(suppression["conditions_json"]))
                    if "reminder" not in events:
                        db.execute(
                            "UPDATE notification_suppression_state SET next_reminder_at = NULL WHERE rule_id=? AND source_id=? AND server=? AND backend=? AND type=? AND host=? AND service=? AND episode_id=?",
                            tuple(suppression[k] for k in ("rule_id", "source_id", "server", "backend", "type", "host", "service", "episode_id")),
                        )
                        continue
                    alert = {
                        "server": active["server"], "backend": active["backend"], "type": active["type"],
                        "host": active["host"], "service": active["service"], "state": active["state"],
                        "hard": bool(active["hard"]), "acknowledged": bool(active["acknowledged"]),
                        "downtime": bool(active["downtime"]),
                    }
                    context = {
                        "source": active["source_name"], "source_id": active["source_id"], **alert,
                    }
                    if not rule_matches(context, conditions):
                        db.execute(
                            "UPDATE notification_suppression_state SET next_reminder_at = NULL WHERE rule_id=? AND source_id=? AND server=? AND backend=? AND type=? AND host=? AND service=? AND episode_id=?",
                            tuple(suppression[k] for k in ("rule_id", "source_id", "server", "backend", "type", "host", "service", "episode_id")),
                        )
                        continue
                    has_enabled_channel = db.execute(
                        """SELECT 1 FROM notification_rule_channels rc JOIN notification_channels c ON c.id=rc.channel_id
                           WHERE rc.rule_id=? AND c.enabled=1 LIMIT 1""", (suppression["rule_id"],),
                    ).fetchone()
                    if not has_enabled_channel:
                        later = _text(current_time + timedelta(seconds=suppression["reminder_interval_seconds"]))
                        db.execute(
                            "UPDATE notification_suppression_state SET next_reminder_at=? WHERE rule_id=? AND source_id=? AND server=? AND backend=? AND type=? AND host=? AND service=? AND episode_id=?",
                            (later, *tuple(suppression[k] for k in ("rule_id", "source_id", "server", "backend", "type", "host", "service", "episode_id"))),
                        )
                        continue
                    reminder_revision = db.execute(
                        """SELECT COALESCE(MAX(revision), 0) + 1 FROM notification_events
                           WHERE source_id=? AND server=? AND backend=? AND type=? AND host=? AND service=?
                             AND episode_id=? AND event_type='reminder'""",
                        (active["source_id"], active["server"], active["backend"], active["type"],
                         active["host"], active["service"], active["episode_id"]),
                    ).fetchone()[0]
                    record_transition(
                        db, source_id=active["source_id"], alert=alert,
                        episode_id=active["episode_id"], revision=reminder_revision,
                        event_type="reminder", old_state=None, occurred_at=now_value,
                        only_rule_id=suppression["rule_id"],
                    )
                    later = _text(current_time + timedelta(seconds=suppression["reminder_interval_seconds"]))
                    db.execute(
                        """UPDATE notification_suppression_state SET next_reminder_at=?, reminder_count=reminder_count+1
                           WHERE rule_id=? AND source_id=? AND server=? AND backend=? AND type=? AND host=? AND service=? AND episode_id=?""",
                        (later, *tuple(suppression[k] for k in ("rule_id", "source_id", "server", "backend", "type", "host", "service", "episode_id"))),
                    )
                    scheduled += 1
                except (ValueError, TypeError, json.JSONDecodeError):
                    LOGGER.warning("Skipping reminder for invalid stored rule %s", suppression["rule_id"])
            return scheduled


async def _main() -> None:
    settings = Settings.from_environment()
    worker = NotificationWorker(
        settings.database_path,
        offline_after_seconds=settings.offline_after_seconds,
    )
    await worker.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
