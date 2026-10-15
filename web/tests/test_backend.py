import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db import MIGRATIONS
from app.main import create_app


def alert(state="CRITICAL", host="db01", service="disk", **changes):
    value = {
        "server": "Thruk oficina", "backend": "alpha", "host": host, "service": service,
        "type": "service" if service else "host", "state": state, "hard": True,
        "acknowledged": False, "downtime": False,
        "last_check": "2026-10-14T21:59:00Z", "last_change": "2026-10-14T21:30:00Z",
    }
    value.update(changes)
    return value


def snapshot(alerts=None, *, timestamp="2026-10-14T22:00:00.000Z", complete=True, servers=None, source="NaftaMon"):
    if servers is None:
        servers = [{"server": "Thruk oficina", "status": "ok"}]
    return {"timestamp": timestamp, "source": source, "complete": complete, "servers": servers, "alerts": alerts or []}


def test_valid_webhook_and_unauthenticated_read_apis(client):
    body = json.dumps(snapshot([alert()])).encode("utf-8")
    response = client.post(
        "/api/v1/ingest",
        content=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "NaftaMon WebhookGateway",
        },
    )
    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert response.json()["kind"] == "snapshot"
    assert client.get("/api/v1/alerts").json()["items"][0]["state"] == "CRITICAL"
    assert client.get("/api/v1/sources").status_code == 200
    assert client.get("/api/v1/stats").json()["alerts"]["total"] == 1
    assert client.get("/api/v1/history").json()["items"][0]["event"] == "opened"
    assert client.get("/healthz").json() == {"status": "ok", "database": "ok"}


def test_invalid_json_content_type_and_shape(client):
    assert client.post("/api/v1/ingest", content="{broken", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/v1/ingest", content="{}", headers={"content-type": "text/plain"}).status_code == 415
    assert client.post("/api/v1/ingest", json={"unexpected": True}).status_code == 422
    bad_timestamp = snapshot([alert()])
    bad_timestamp["timestamp"] = "yesterday"
    assert client.post("/api/v1/ingest", json=bad_timestamp).status_code == 422


def test_request_body_size_limit(app_factory):
    app = app_factory(max_request_bytes=128)
    with TestClient(app) as client:
        response = client.post("/api/v1/ingest", content=b" " * 129,
                               headers={"content-type": "application/json", "content-length": "129"})
    assert response.status_code == 413


def test_repeated_snapshot_refreshes_source_without_duplicate_alert_or_history(client):
    body = snapshot([alert()])
    first = client.post("/api/v1/ingest", json=body)
    time.sleep(0.02)
    second = client.post("/api/v1/ingest", json=body)
    assert first.status_code == second.status_code == 202
    assert client.get("/api/v1/alerts").json()["total"] == 1
    history = client.get("/api/v1/history").json()["items"]
    assert [item["event"] for item in history] == ["opened"]
    source = client.get("/api/v1/sources").json()["items"][0]
    assert source["last_received_at"] >= first.json()["received_at"]


def test_empty_snapshot_resolves_alert_and_records_transition(client):
    assert client.post("/api/v1/ingest", json=snapshot([alert()])).status_code == 202
    empty = snapshot([], timestamp="2026-10-14T22:01:00.000Z")
    assert client.post("/api/v1/ingest", json=empty).status_code == 202
    assert client.get("/api/v1/alerts").json()["total"] == 0
    events = {item["event"] for item in client.get("/api/v1/history").json()["items"]}
    assert events == {"opened", "recovered"}


def test_state_and_flag_changes_are_recorded_without_duplicate_current_alert(client):
    client.post("/api/v1/ingest", json=snapshot([alert()]))
    updated = alert("WARNING", acknowledged=True, last_check="2026-10-14T22:00:00Z")
    client.post("/api/v1/ingest", json=snapshot([updated], timestamp="2026-10-14T22:01:00Z"))
    current = client.get("/api/v1/alerts").json()
    assert current["total"] == 1
    assert current["items"][0]["state"] == "WARNING"
    assert current["items"][0]["acknowledged"] is True
    events = {item["event"] for item in client.get("/api/v1/history").json()["items"]}
    assert events == {"opened", "state_changed", "attributes_changed"}


def test_partial_snapshot_reconciles_ok_and_preserves_error_server_alerts(app_factory):
    app = app_factory()
    with TestClient(app) as client:
        body = {
            "timestamp": "2026-10-14T22:00:00Z", "source": "NaftaMon", "complete": False,
            "servers": [{"server": "Thruk oficina", "status": "ok"}, {"server": "Thruk dc", "status": "error"}],
            "alerts": [alert(), {
                "server": "Thruk dc", "backend": "beta", "host": "old", "service": "cpu", "type": "service",
                "state": "WARNING", "hard": True, "acknowledged": False, "downtime": False,
                "last_check": "2026-10-14T21:59:00Z", "last_change": "2026-10-14T21:30:00Z",
            }],
        }
        # The real Gateway omits alerts for error/pending servers; establish the cached alert first.
        initial = snapshot([{
            "server": "Thruk dc", "backend": "beta", "host": "old", "service": "cpu", "type": "service",
            "state": "WARNING", "hard": True, "acknowledged": False, "downtime": False,
            "last_check": "2026-10-14T21:59:00Z", "last_change": "2026-10-14T21:30:00Z",
        }], servers=[{"server": "Thruk dc", "status": "ok"}])
        assert client.post("/api/v1/ingest", json=initial).status_code == 202
        body["alerts"] = [alert()]
        assert client.post("/api/v1/ingest", json=body).status_code == 202
        items = client.get("/api/v1/alerts").json()["items"]
        assert {row["host"] for row in items} == {"db01", "old"}
        assert next(row for row in items if row["host"] == "old")["stale"] is True
        assert next(row for row in items if row["host"] == "db01")["stale"] is False


def test_partial_snapshot_marks_omitted_server_stale_when_receipt_times_match(client, monkeypatch):
    same_received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    monkeypatch.setattr("app.ingest.iso_utc", lambda value=None: same_received_at)
    first = snapshot(
        [alert(host="old-db", server="Thruk A")],
        timestamp=same_received_at,
        servers=[{"server": "Thruk A", "status": "ok"}, {"server": "Thruk B", "status": "ok"}],
    )
    assert client.post("/api/v1/ingest", json=first).status_code == 202

    partial = snapshot(
        [alert(host="new-db", server="Thruk B")],
        timestamp=same_received_at,
        complete=False,
        servers=[{"server": "Thruk B", "status": "ok"}, {"server": "Thruk C", "status": "error"}],
    )
    assert client.post("/api/v1/ingest", json=partial).status_code == 202

    items = {row["host"]: row for row in client.get("/api/v1/alerts").json()["items"]}
    assert items["old-db"]["stale"] is True
    assert items["new-db"]["stale"] is False


def test_concurrent_snapshots_from_same_source_are_serialized(app_factory):
    app = app_factory()
    peer = ("10.0.0.50", 5000)
    initial = snapshot(
        [alert(host="old-db", server="Thruk A")],
        timestamp="2026-10-14T22:00:00.000Z",
        servers=[{"server": "Thruk A", "status": "ok"}, {"server": "Thruk B", "status": "ok"}],
    )
    full = snapshot(
        [alert(host="old-db", server="Thruk A"), alert(host="db-b", server="Thruk B")],
        timestamp="2026-10-14T22:01:00.000Z",
        servers=[{"server": "Thruk A", "status": "ok"}, {"server": "Thruk B", "status": "ok"}],
    )
    partial = snapshot(
        [alert(host="db-b", server="Thruk B")],
        timestamp="2026-10-14T22:02:00.000Z",
        complete=False,
        servers=[{"server": "Thruk B", "status": "ok"}, {"server": "Thruk C", "status": "error"}],
    )

    with TestClient(app, client=peer) as setup_client:
        assert setup_client.post("/api/v1/ingest", json=initial).status_code == 202

    def send(body):
        with TestClient(app, client=peer) as concurrent_client:
            return concurrent_client.post("/api/v1/ingest", json=body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send, (full, partial)))

    assert [response.status_code for response in responses] == [202, 202]
    with TestClient(app, client=peer) as verify_client:
        sources = verify_client.get("/api/v1/sources").json()["items"]
        assert len(sources) == 1
        latest_timestamp = sources[0]["payload_timestamp"]
        latest_received_at = sources[0]["last_received_at"]
        alerts = {row["host"]: row for row in verify_client.get("/api/v1/alerts").json()["items"]}
    assert alerts["old-db"]["stale"] is (latest_timestamp == partial["timestamp"])
    assert alerts["db-b"]["stale"] is False
    assert latest_received_at == max(response.json()["received_at"] for response in responses)


def test_test_webhook_does_not_create_source_or_replace_snapshot(client):
    client.post("/api/v1/ingest", json=snapshot([alert()]))
    before = client.get("/api/v1/sources").json()
    test_message = {
        "type": "test", "test": True, "source": "NaftaMon", "timestamp": "2026-10-14T22:03:00.000Z",
        "message": "NaftaMon test webhook",
    }
    response = client.post("/api/v1/ingest", json=test_message)
    assert response.status_code == 202
    assert response.json()["kind"] == "test"
    assert client.get("/api/v1/sources").json() == before
    assert client.get("/api/v1/alerts").json()["total"] == 1
    invalid_source = {**test_message, "source": "unrecognized-sender"}
    assert client.post("/api/v1/ingest", json=invalid_source).status_code == 422


def test_observed_peer_ip_is_only_a_weak_fallback_for_grouping(app_factory):
    app = app_factory()
    with TestClient(app, client=("10.0.0.10", 5000)) as first_peer, \
         TestClient(app, client=("10.0.0.11", 5000)) as second_peer:
        body = snapshot([alert()])
        assert first_peer.post("/api/v1/ingest", json=body, headers={"x-forwarded-for": "203.0.113.90"}).status_code == 202
        assert first_peer.post("/api/v1/ingest", json=body).status_code == 202
        assert len(first_peer.get("/api/v1/sources").json()["items"]) == 1
        assert second_peer.post("/api/v1/ingest", json=body).status_code == 202
        sources = first_peer.get("/api/v1/sources").json()["items"]
        assert {source["peer_ip"] for source in sources} == {"10.0.0.10", "10.0.0.11"}
        assert first_peer.get("/api/v1/alerts").json()["total"] == 2


def test_offline_source_retains_last_known_alert_and_history(app_factory):
    app = app_factory(offline_after_seconds=1)
    with TestClient(app) as client:
        client.post("/api/v1/ingest", json=snapshot([alert()]))
        time.sleep(1.1)
        source = client.get("/api/v1/sources").json()["items"][0]
        assert source["offline"] is True
        current = client.get("/api/v1/alerts").json()["items"]
        assert len(current) == 1 and current[0]["stale"] is True
        assert [item["event"] for item in client.get("/api/v1/history").json()["items"]] == ["opened"]


def test_database_survives_app_recreation_and_filters_are_parameterized(tmp_path):
    db_path = tmp_path / "persist.sqlite3"
    app1 = create_app(database_path=db_path)
    with TestClient(app1) as first:
        first.post("/api/v1/ingest", json=snapshot([alert()]))
    app2 = create_app(database_path=db_path)
    with TestClient(app2) as restarted:
        assert restarted.get("/api/v1/alerts", params={"server": "Thruk oficina"}).json()["total"] == 1
        assert restarted.get("/api/v1/alerts", params={"server": "' OR 1=1 --"}).json()["total"] == 0
        assert restarted.get("/api/v1/history", params={"limit": 1, "offset": 0}).json()["total"] == 1


def test_semantically_invalid_server_and_alert_relationship(client):
    body = snapshot([alert()], servers=[{"server": "Thruk oficina", "status": "error"}], complete=False)
    assert client.post("/api/v1/ingest", json=body).status_code == 422
    duplicate = snapshot([alert(), alert()])
    assert client.post("/api/v1/ingest", json=duplicate).status_code == 422
    wrong_source = snapshot([alert()], source="another-sender")
    assert client.post("/api/v1/ingest", json=wrong_source).status_code == 422


def test_healthcheck_rejects_missing_database_schema(app_factory, tmp_path):
    database_path = tmp_path / "deleted.sqlite3"
    app = app_factory(database_path=database_path)
    with TestClient(app) as client:
        database_path.unlink()
        response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"status": "unhealthy", "database": "unavailable"}


def test_v1_database_migration_preserves_stale_server_state(tmp_path):
    database_path = tmp_path / "legacy-v1.sqlite3"
    now = datetime.now(timezone.utc)
    older_status = now.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    latest_snapshot = now.replace(microsecond=0) + timedelta(seconds=10)
    latest_received = latest_snapshot.isoformat().replace("+00:00", "Z")
    alert_row = alert(host="legacy-db")

    with sqlite3.connect(database_path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        for statement in MIGRATIONS[1]:
            db.execute(statement)
        db.execute("INSERT INTO schema_version(version) VALUES (1)")
        db.execute(
            """INSERT INTO sources
               (id, source_name, peer_ip, first_received_at, last_received_at, payload_timestamp, complete)
               VALUES (1, 'NaftaMon', '10.0.0.60', ?, ?, ?, 0)""",
            (older_status, latest_received, latest_received),
        )
        db.execute(
            """INSERT INTO latest_snapshots
               (source_id, payload_json, content_hash, payload_timestamp, received_at, complete)
               VALUES (1, '{}', 'legacy-hash', ?, ?, 0)""",
            (latest_received, latest_received),
        )
        db.execute(
            """INSERT INTO monitored_servers
               (source_id, name, status, status_received_at, last_ok_at, removed)
               VALUES (1, 'Thruk oficina', 'ok', ?, ?, 0)""",
            (older_status, older_status),
        )
        db.execute(
            """INSERT INTO active_alerts
               (source_id, server, backend, host, service, type, state, hard, acknowledged, downtime,
                last_check, last_change, first_seen_at, last_seen_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("Thruk oficina", alert_row["backend"], alert_row["host"], alert_row["service"], alert_row["type"],
             alert_row["state"], 1, 0, 0, alert_row["last_check"], alert_row["last_change"], older_status, older_status),
        )

    app = create_app(database_path=database_path, offline_after_seconds=3600)
    with TestClient(app) as client:
        migrated_alert = client.get("/api/v1/alerts").json()["items"][0]
        assert migrated_alert["stale"] is True
        assert client.get("/healthz").json() == {"status": "ok", "database": "ok"}


def test_v2_migration_preserves_revision_based_stale_state(tmp_path):
    database_path = tmp_path / "legacy-v2.sqlite3"
    received_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    alert_row = alert(host="legacy-v2-db")
    snapshot_json = json.dumps({
        "timestamp": received_at,
        "source": "NaftaMon",
        "complete": False,
        "servers": [{"server": "Thruk B", "status": "ok"}, {"server": "Thruk C", "status": "error"}],
        "alerts": [],
    })

    with sqlite3.connect(database_path) as db:
        db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
        for version in (1, 2):
            for statement in MIGRATIONS[version]:
                db.execute(statement)
            db.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))
        db.execute(
            """INSERT INTO sources
               (id, source_name, peer_ip, first_received_at, last_received_at, payload_timestamp, complete, snapshot_revision)
               VALUES (1, 'NaftaMon', '10.0.0.61', ?, ?, ?, 0, 2)""",
            (received_at, received_at, received_at),
        )
        db.execute(
            """INSERT INTO latest_snapshots
               (source_id, payload_json, content_hash, payload_timestamp, received_at, complete)
               VALUES (1, ?, 'v2-hash', ?, ?, 0)""",
            (snapshot_json, received_at, received_at),
        )
        db.execute(
            """INSERT INTO monitored_servers
               (source_id, name, status, status_received_at, last_ok_at, removed, status_revision)
               VALUES (1, 'Thruk oficina', 'ok', ?, ?, 0, 1)""",
            (received_at, received_at),
        )
        db.execute(
            """INSERT INTO active_alerts
               (source_id, server, backend, host, service, type, state, hard, acknowledged, downtime,
                last_check, last_change, first_seen_at, last_seen_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("Thruk oficina", alert_row["backend"], alert_row["host"], alert_row["service"], alert_row["type"],
             alert_row["state"], 1, 0, 0, alert_row["last_check"], alert_row["last_change"], received_at, received_at),
        )

    app = create_app(database_path=database_path, offline_after_seconds=3600)
    with TestClient(app) as client:
        migrated_alert = client.get("/api/v1/alerts").json()["items"][0]
    assert migrated_alert["stale"] is True
