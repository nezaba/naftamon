from contextlib import contextmanager
import sqlite3
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1

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
}


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
