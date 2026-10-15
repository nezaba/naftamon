import time

from fastapi.testclient import TestClient

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
    response = client.post("/api/v1/ingest", json=snapshot([alert()]))
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
