from contextlib import contextmanager
import sqlite3
import unicodedata
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 12

# Add future schema changes as transactional SQL statements keyed by version.
MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            source_name TEXT NOT NULL,
            peer_ip TEXT NOT NULL,
            first_received_at TEXT NOT NULL,
            last_received_at TEXT NOT NULL,
            payload_timestamp TEXT NOT NULL,
            complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
            UNIQUE (source_name, peer_ip)
        )""",
        """CREATE TABLE latest_snapshots (
            source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            payload_timestamp TEXT NOT NULL,
            received_at TEXT NOT NULL,
            complete INTEGER NOT NULL CHECK (complete IN (0, 1))
        )""",
        """CREATE TABLE monitored_servers (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'pending', 'removed')),
            status_received_at TEXT NOT NULL,
            last_ok_at TEXT,
            removed INTEGER NOT NULL DEFAULT 0 CHECK (removed IN (0, 1)),
            UNIQUE (source_id, name)
        )""",
        """CREATE TABLE active_alerts (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            server TEXT NOT NULL,
            backend TEXT NOT NULL,
            host TEXT NOT NULL,
            service TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('host', 'service')),
            state TEXT NOT NULL,
            hard INTEGER NOT NULL CHECK (hard IN (0, 1)),
            acknowledged INTEGER NOT NULL CHECK (acknowledged IN (0, 1)),
            downtime INTEGER NOT NULL CHECK (downtime IN (0, 1)),
            last_check TEXT NOT NULL,
            last_change TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            UNIQUE (source_id, server, backend, type, host, service)
        )""",
        """CREATE TABLE alert_history (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            server TEXT NOT NULL,
            backend TEXT NOT NULL,
            host TEXT NOT NULL,
            service TEXT NOT NULL,
            type TEXT NOT NULL,
            event TEXT NOT NULL CHECK (event IN ('opened', 'state_changed', 'attributes_changed', 'recovered')),
            old_value TEXT,
            new_value TEXT,
            observed_at TEXT NOT NULL
        )""",
        "CREATE INDEX idx_alerts_source_server ON active_alerts(source_id, server)",
        "CREATE INDEX idx_history_observed ON alert_history(observed_at DESC)",
        "CREATE INDEX idx_history_source_server ON alert_history(source_id, server)",
    ),
    2: (
        "ALTER TABLE sources ADD COLUMN snapshot_revision INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE monitored_servers ADD COLUMN status_revision INTEGER NOT NULL DEFAULT 0",
    ),
    3: (
        # Preserve the stale/fresh interpretation when upgrading a v1 database
        # whose revision columns are initialized from timestamps and snapshot contents.
        """UPDATE monitored_servers SET status_revision = CASE WHEN status_received_at < (
               SELECT last_received_at FROM sources WHERE sources.id = monitored_servers.source_id
           ) OR NOT EXISTS (
               SELECT 1 FROM latest_snapshots AS latest,
                 json_each(latest.payload_json, '$.servers') AS incoming
               WHERE latest.source_id = monitored_servers.source_id
                 AND json_extract(incoming.value, '$.server') = monitored_servers.name
           ) THEN 0 ELSE 1 END
           WHERE status_revision = 0 AND source_id IN (
               SELECT id FROM sources WHERE snapshot_revision = 0
           )""",
        "UPDATE sources SET snapshot_revision = 1 WHERE snapshot_revision = 0",
    ),
    4: (
        "ALTER TABLE active_alerts ADD COLUMN episode_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE active_alerts ADD COLUMN notification_revision INTEGER NOT NULL DEFAULT 0",
        "UPDATE active_alerts SET episode_id = 'legacy-' || id WHERE episode_id = ''",
        """CREATE TABLE notification_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            history_retention_days INTEGER NOT NULL DEFAULT 90 CHECK (history_retention_days >= 0),
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE notification_channels (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            provider TEXT NOT NULL CHECK (provider IN ('telegram', 'gotify', 'ntfy', 'webhook')),
            config_json TEXT NOT NULL,
            secrets_ciphertext TEXT,
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            timeout_seconds INTEGER NOT NULL DEFAULT 10 CHECK (timeout_seconds BETWEEN 1 AND 60),
            rate_limit_seconds INTEGER NOT NULL DEFAULT 1 CHECK (rate_limit_seconds >= 0),
            last_attempt_at TEXT,
            last_result TEXT,
            last_error TEXT,
            last_success_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE notification_rules (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            conditions_json TEXT NOT NULL,
            events_json TEXT NOT NULL,
            cooldown_seconds INTEGER NOT NULL DEFAULT 0 CHECK (cooldown_seconds >= 0),
            reminder_interval_seconds INTEGER CHECK (reminder_interval_seconds IS NULL OR reminder_interval_seconds > 0),
            subject_template TEXT NOT NULL,
            body_template TEXT NOT NULL,
            match_count INTEGER NOT NULL DEFAULT 0,
            last_matched_at TEXT,
            last_triggered_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""",
        """CREATE TABLE notification_rule_channels (
            rule_id INTEGER NOT NULL REFERENCES notification_rules(id) ON DELETE CASCADE,
            channel_id INTEGER NOT NULL REFERENCES notification_channels(id) ON DELETE CASCADE,
            PRIMARY KEY (rule_id, channel_id)
        )""",
        """CREATE TABLE notification_events (
            id TEXT PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            server TEXT NOT NULL,
            backend TEXT NOT NULL,
            type TEXT NOT NULL,
            host TEXT NOT NULL,
            service TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN ('opened', 'state_changed', 'recovered', 'reminder')),
            state TEXT NOT NULL,
            old_state TEXT,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (source_id, server, backend, type, host, service, episode_id, revision, event_type)
        )""",
        """CREATE TABLE notification_deliveries (
            id INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES notification_events(id) ON DELETE CASCADE,
            rule_id INTEGER REFERENCES notification_rules(id) ON DELETE SET NULL,
            rule_name TEXT NOT NULL,
            channel_id INTEGER REFERENCES notification_channels(id) ON DELETE SET NULL,
            channel_name TEXT NOT NULL,
            provider TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'in_flight', 'sent', 'failed', 'cancelled')),
            attempt_count INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 5,
            next_attempt_at TEXT NOT NULL,
            lease_until TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (event_id, rule_id, channel_id)
        )""",
        """CREATE TABLE notification_attempts (
            id INTEGER PRIMARY KEY,
            delivery_id INTEGER NOT NULL REFERENCES notification_deliveries(id) ON DELETE CASCADE,
            attempt_number INTEGER NOT NULL,
            attempted_at TEXT NOT NULL,
            finished_at TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('sent', 'retry', 'failed')),
            http_status INTEGER,
            error TEXT,
            duration_ms INTEGER NOT NULL,
            UNIQUE (delivery_id, attempt_number)
        )""",
        """CREATE TABLE notification_suppression_state (
            rule_id INTEGER NOT NULL REFERENCES notification_rules(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            server TEXT NOT NULL,
            backend TEXT NOT NULL,
            type TEXT NOT NULL,
            host TEXT NOT NULL,
            service TEXT NOT NULL,
            episode_id TEXT NOT NULL,
            last_sent_at TEXT,
            next_reminder_at TEXT,
            reminder_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (rule_id, source_id, server, backend, type, host, service, episode_id)
        )""",
        "CREATE INDEX idx_notification_deliveries_due ON notification_deliveries(status, next_attempt_at, lease_until)",
        "CREATE INDEX idx_notification_events_date ON notification_events(occurred_at DESC)",
        "CREATE INDEX idx_notification_attempts_delivery ON notification_attempts(delivery_id, attempt_number DESC)",
        "INSERT INTO notification_settings(id, enabled, history_retention_days, updated_at) VALUES (1, 0, 90, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
    ),
    5: (
        "ALTER TABLE active_alerts ADD COLUMN status_information TEXT NOT NULL DEFAULT ''",
        """CREATE TABLE monitored_statuses (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
            server TEXT NOT NULL,
            backend TEXT NOT NULL,
            host TEXT NOT NULL,
            service TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('host', 'service')),
            state TEXT NOT NULL CHECK (state = 'UP'),
            status_information TEXT NOT NULL DEFAULT '',
            hard INTEGER NOT NULL CHECK (hard IN (0, 1)),
            acknowledged INTEGER NOT NULL CHECK (acknowledged IN (0, 1)),
            downtime INTEGER NOT NULL CHECK (downtime IN (0, 1)),
            last_check TEXT NOT NULL,
            last_change TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            snapshot_timestamp TEXT NOT NULL,
            UNIQUE (source_id, server, backend, type, host, service)
        )""",
        "CREATE INDEX idx_monitored_statuses_source_server ON monitored_statuses(source_id, server)",
    ),
    6: (
        "ALTER TABLE active_alerts ADD COLUMN status_information_available INTEGER NOT NULL DEFAULT 0 CHECK (status_information_available IN (0, 1))",
        # Existing non-empty values are known to have been received; empty legacy
        # values are ambiguous, so leave them unavailable and prevent false matches.
        "UPDATE active_alerts SET status_information_available = 1 WHERE status_information <> ''",
    ),
    7: (
        # Retain a minimal source tombstone so notification history can keep its
        # source_id foreign key while all current monitoring state is removed.
        "ALTER TABLE sources ADD COLUMN deleted_at TEXT",
        "CREATE INDEX idx_sources_deleted_source_peer ON sources(deleted_at, source_name, peer_ip)",
    ),
    8: (
        """CREATE TABLE notification_config_backups (
            id INTEGER PRIMARY KEY,
            created_at TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            secrets_ciphertext TEXT
        )""",
        "CREATE INDEX idx_notification_config_backups_created ON notification_config_backups(id DESC)",
    ),
    9: (
        "ALTER TABLE latest_snapshots ADD COLUMN payload_gzip BLOB",
        "ALTER TABLE latest_snapshots ADD COLUMN payload_encoding TEXT NOT NULL DEFAULT 'json'",
        "CREATE INDEX idx_active_alerts_state ON active_alerts(state, source_id)",
        "CREATE INDEX idx_monitored_statuses_state ON monitored_statuses(state, source_id)",
        "CREATE INDEX idx_monitored_statuses_server_backend ON monitored_statuses(server, backend, source_id)",
    ),
    10: (
        "CREATE TABLE app_state (key TEXT PRIMARY KEY, value INTEGER NOT NULL)",
        "INSERT INTO app_state(key, value) VALUES ('monitoring_revision', 0)",
    ),
    11: (
        "ALTER TABLE active_alerts ADD COLUMN search_text TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE active_alerts ADD COLUMN host_search TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE active_alerts ADD COLUMN service_search TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE monitored_statuses ADD COLUMN search_text TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE monitored_statuses ADD COLUMN host_search TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE monitored_statuses ADD COLUMN service_search TEXT NOT NULL DEFAULT ''",
        "UPDATE active_alerts SET search_text = nafta_fold(server || ' ' || backend || ' ' || host || ' ' || service || ' ' || type || ' ' || state || ' ' || status_information), host_search = nafta_fold(host), service_search = nafta_fold(service)",
        "UPDATE monitored_statuses SET search_text = nafta_fold(server || ' ' || backend || ' ' || host || ' ' || service || ' ' || type || ' ' || state || ' ' || status_information), host_search = nafta_fold(host), service_search = nafta_fold(service)",
        "CREATE INDEX idx_active_alerts_host_search ON active_alerts(host_search, source_id)",
        "CREATE INDEX idx_monitored_statuses_host_search ON monitored_statuses(host_search, source_id)",
    ),
    12: (
        # Track whether the current gateway snapshot included the UP-status array.
        # This keeps old rows stale when legacy senders omit it, without touching
        # every status row just to advance a per-row timestamp on each poll.
        "ALTER TABLE monitored_servers ADD COLUMN status_data_revision INTEGER NOT NULL DEFAULT 0",
    ),
}


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.create_function("nafta_fold", 1, _fold_search, deterministic=True)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _fold_search(value: str | None) -> str:
    if value is None:
        return ""
    decomposed = unicodedata.normalize("NFD", str(value).casefold())
    return "".join(char for char in decomposed if unicodedata.category(char) != "Mn")


def initialize(path: Path) -> None:
    with connect(path) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        current = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
        if current > SCHEMA_VERSION:
            raise RuntimeError("Database schema is newer than this NaftaMon Web build")
        for version in range(current + 1, SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(version)
            if statements is None:
                raise RuntimeError(f"No migration is defined for database schema version {version}")
            for statement in statements:
                db.execute(statement)
            db.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
