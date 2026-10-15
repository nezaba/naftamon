import anyio
import ipaddress
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.db import MIGRATIONS, connect, initialize
from app.main import create_app
from app.notifications import api as notification_api
from app.notifications.crypto import decrypt_secrets
from app.notifications.providers import (
    DeliveryResult,
    PinnedAddressTransport,
    send_notification,
)
from app.notifications.security import (
    DestinationRejected,
    prepare_destination_url,
    validate_destination_url,
)
from app.notifications.worker import NotificationWorker


def _run(awaitable):
    async def execute():
        return await awaitable
    return anyio.run(execute)


@pytest.fixture
def key_file(tmp_path, monkeypatch):
    path = tmp_path / "notification.key"
    path.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("NOTIFICATION_KEY_FILE", str(path))
    return path


@pytest.fixture
def public_dns(monkeypatch):
    from app.notifications import security
    monkeypatch.setattr(
        security, "resolve_host",
        lambda host: {ipaddress.ip_address("93.184.216.34")},
    )


def _channel_payload(*, name="test sink", rate_limit_seconds=0):
    return {
        "name": name,
        "provider": "webhook",
        "config": {
            "url": "https://sink.example.test/hook",
            "header_names": ["X-Lab-Secret"],
            "json_template": {"event": "{event_type}", "host": "{host}", "state": "{state}"},
        },
        "secrets": {"headers": {"X-Lab-Secret": "secret-value-never-returned"}},
        "enabled": True,
        "timeout_seconds": 3,
        "rate_limit_seconds": rate_limit_seconds,
    }


def _rule_payload(channel_id, **overrides):
    value = {
        "name": "all alert transitions",
        "enabled": True,
        "conditions": {
            "operator": "AND",
            "conditions": [{"field": "host", "operator": "equals", "value": "db01"}],
        },
        "events": ["opened", "state_changed", "recovered", "reminder"],
        "channel_ids": [channel_id],
        "cooldown_seconds": 0,
        "reminder_interval_seconds": 1,
    }
    value.update(overrides)
    return value


def _alert(state="CRITICAL"):
    return {
        "server": "Thruk oficina", "backend": "alpha", "host": "db01", "service": "disk",
        "type": "service", "state": state, "hard": True, "acknowledged": False, "downtime": False,
        "last_check": "2026-10-14T21:59:00Z", "last_change": "2026-10-14T21:30:00Z",
    }


def _snapshot(alerts, *, status="ok", complete=None, timestamp="2026-10-14T22:00:00Z"):
    if complete is None:
        complete = status == "ok"
    return {
        "timestamp": timestamp, "source": "NaftaMon", "complete": complete,
        "servers": [{"server": "Thruk oficina", "status": status}], "alerts": alerts,
    }


def test_notification_api_secrets_and_test_rate_limit(app_factory, key_file, public_dns, monkeypatch):
    app = app_factory()

    async def fake_send(*args, **kwargs):
        return DeliveryResult(True, False, status_code=202)

    monkeypatch.setattr(notification_api, "send_notification", fake_send)
    with TestClient(app) as client:
        # The notification APIs are deliberately unauthenticated, like the dashboard.
        response = client.post("/api/v1/notifications/channels", json=_channel_payload(rate_limit_seconds=60))
        assert response.status_code == 201
        channel = response.json()
        assert channel["secret_configured"] is True
        assert "secret-value-never-returned" not in response.text
        assert "secrets" not in channel

        listed = client.get("/api/v1/notifications/channels")
        assert listed.status_code == 200
        assert "secret-value-never-returned" not in listed.text
        assert "secrets_ciphertext" not in listed.text

        first = client.post(f"/api/v1/notifications/channels/{channel['id']}/test")
        second = client.post(f"/api/v1/notifications/channels/{channel['id']}/test")
        assert first.status_code == 200
        assert first.json() == {"ok": True, "status_code": 202, "error": None, "retry_after_seconds": None}
        assert second.status_code == 429
        assert int(second.headers["retry-after"]) >= 1

        assert client.get("/api/v1/notifications/settings").json()["enabled"] is False
        settings = client.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 30})
        assert settings.status_code == 200

    with connect(app.state.settings.database_path) as db:
        ciphertext = db.execute("SELECT secrets_ciphertext FROM notification_channels").fetchone()[0]
        assert "secret-value-never-returned" not in ciphertext
        assert decrypt_secrets(ciphertext)["headers"]["X-Lab-Secret"] == "secret-value-never-returned"


def test_invalid_destination_and_template_are_validation_errors(app_factory, public_dns):
    app = app_factory()
    payload = _channel_payload()
    payload["config"]["url"] = "http://127.0.0.1:8080/hook"
    with TestClient(app) as client:
        response = client.post("/api/v1/notifications/channels", json=payload)
        assert response.status_code == 422
    payload = _channel_payload()
    payload["config"]["json_template"] = {"value": "{unknown_field}"}
    with TestClient(app) as client:
        response = client.post("/api/v1/notifications/channels", json=payload)
        assert response.status_code == 422


def test_rule_dry_run_crud_and_paginated_api_contract(app_factory, key_file, public_dns):
    app = app_factory()
    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        rule_response = client.post("/api/v1/notifications/rules", json=_rule_payload(channel["id"]))
        assert rule_response.status_code == 201
        rule = rule_response.json()
        rules = client.get("/api/v1/notifications/rules?limit=1&offset=0")
        assert rules.status_code == 200
        assert (rules.json()["total"], rules.json()["limit"], rules.json()["offset"]) == (1, 1, 0)
        assert rules.json()["items"][0]["channel_ids"] == [channel["id"]]

        assert client.post("/api/v1/ingest", json=_snapshot([_alert()])).status_code == 202
        dry_run = client.post(f"/api/v1/notifications/rules/{rule['id']}/test")
        assert dry_run.status_code == 200
        assert dry_run.json()["dry_run"] is True
        assert dry_run.json()["sent"] is False
        assert dry_run.json()["match_count"] == 1

        duplicate = client.post(f"/api/v1/notifications/rules/{rule['id']}/duplicate")
        assert duplicate.status_code == 201
        assert duplicate.json()["enabled"] is False
        history = client.get("/api/v1/notifications/history?limit=10&offset=0")
        assert history.status_code == 200
        assert {"items", "total", "limit", "offset"}.issubset(history.json())

        updated_payload = _rule_payload(channel["id"], enabled=False)
        updated = client.put(f"/api/v1/notifications/rules/{rule['id']}", json=updated_payload)
        assert updated.status_code == 200
        assert updated.json()["enabled"] is False
        assert client.delete(f"/api/v1/notifications/rules/{duplicate.json()['id']}").status_code == 204
        assert client.delete(f"/api/v1/notifications/channels/{channel['id']}").status_code == 204


def test_status_information_rule_persists_across_restart(app_factory, tmp_path):
    database_path = tmp_path / "status-information-rule.sqlite3"
    condition_group = {
        "operator": "AND",
        "conditions": [
            {"field": "status_information", "operator": "not_contains", "value": "maintenance"},
            {"field": "state", "operator": "equals", "value": "CRITICAL"},
        ],
    }
    with TestClient(app_factory(database_path=database_path)) as client:
        payload = _rule_payload(0, channel_ids=[], conditions=condition_group)
        created = client.post("/api/v1/notifications/rules", json=payload)
        assert created.status_code == 201
        rule_id = created.json()["id"]
        assert created.json()["conditions"] == condition_group

    with TestClient(app_factory(database_path=database_path)) as client:
        rules = client.get("/api/v1/notifications/rules").json()["items"]
        restored = next(rule for rule in rules if rule["id"] == rule_id)
        assert restored["conditions"] == condition_group


def test_status_information_rule_dry_run_uses_only_available_alert_data(app_factory, key_file, public_dns):
    app = app_factory()
    conditions = {"operator": "AND", "conditions": [
        {"field": "status_information", "operator": "contains", "value": "DISK CRITICAL"},
    ]}
    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        rule = client.post(
            "/api/v1/notifications/rules",
            json=_rule_payload(channel["id"], conditions=conditions),
        ).json()
        with_info = _alert("CRITICAL") | {"status_information": "disk critical: 90% used"}
        legacy = _alert("CRITICAL") | {"host": "db02"}
        legacy.pop("status_information", None)
        assert client.post("/api/v1/ingest", json=_snapshot([with_info, legacy])).status_code == 202
        result = client.post(f"/api/v1/notifications/rules/{rule['id']}/test")
        assert result.status_code == 200
        assert result.json()["match_count"] == 1
        assert result.json()["sent"] is False


def test_status_information_notifications_are_per_alert_and_ignore_ok_statuses(
    app_factory, key_file, public_dns,
):
    app = app_factory()
    conditions = {
        "operator": "AND",
        "conditions": [
            {"field": "state", "operator": "equals", "value": "CRITICAL"},
            {"field": "host", "operator": "equals", "value": "db01"},
            {"field": "service", "operator": "equals", "value": "disk"},
            {"field": "status_information", "operator": "contains", "value": "connection refused"},
        ],
    }
    matching = _alert("CRITICAL") | {"status_information": "Connection refused by peer"}
    wrong_host = _alert("CRITICAL") | {
        "host": "db02", "status_information": "Connection refused by peer",
    }
    wrong_state = _alert("WARNING") | {
        "host": "db03", "status_information": "Connection refused by peer",
    }
    wrong_text = _alert("CRITICAL") | {
        "host": "db04", "status_information": "All checks passed",
    }
    legacy = _alert("CRITICAL") | {"host": "db05"}
    legacy.pop("status_information", None)
    ok_status = {
        "server": "Thruk oficina", "backend": "alpha", "host": "db01", "service": "",
        "type": "host", "state": "UP", "hard": True, "acknowledged": False, "downtime": False,
        "last_check": "2026-10-14T21:59:00Z", "last_change": "2026-10-14T21:30:00Z",
        "status_information": "Connection refused should not notify from UP statuses",
    }

    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        rule_payload = _rule_payload(channel["id"], conditions=conditions)
        rule = client.post("/api/v1/notifications/rules", json=rule_payload)
        assert rule.status_code == 201
        assert client.put(
            "/api/v1/notifications/settings",
            json={"enabled": True, "history_retention_days": 90},
        ).status_code == 200

        snapshot = _snapshot([matching, wrong_host, wrong_state, wrong_text, legacy])
        snapshot["ok_statuses"] = [ok_status]
        assert client.post("/api/v1/ingest", json=snapshot).status_code == 202

    with connect(app.state.settings.database_path) as db:
        rows = db.execute(
            "SELECT host, status_information_available FROM active_alerts ORDER BY host",
        ).fetchall()
        availability = {row["host"]: row["status_information_available"] for row in rows}
        assert availability["db01"] == 1
        assert availability["db02"] == 1
        assert availability["db05"] == 0
        assert db.execute("SELECT COUNT(*) FROM notification_events WHERE event_type='opened'").fetchone()[0] == 5
        deliveries = db.execute(
            "SELECT e.payload_json FROM notification_deliveries d JOIN notification_events e ON e.id=d.event_id",
        ).fetchall()
        assert len(deliveries) == 1
        context = json.loads(deliveries[0]["payload_json"])
        assert context["host"] == "db01"
        assert context["status_information"] == "Connection refused by peer"
        # UP host/service records are persisted as monitoring state only.
        assert db.execute("SELECT COUNT(*) FROM monitored_statuses").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM notification_events WHERE state='UP'").fetchone()[0] == 0


def test_recovery_rule_matches_last_known_status_information(app_factory, key_file, public_dns):
    app = app_factory()
    conditions = {"operator": "AND", "conditions": [
        {"field": "status_information", "operator": "contains", "value": "timeout"},
    ]}
    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        payload = _rule_payload(
            channel["id"], conditions=conditions, events=["recovered"],
        )
        assert client.post("/api/v1/notifications/rules", json=payload).status_code == 201
        assert client.put(
            "/api/v1/notifications/settings",
            json={"enabled": True, "history_retention_days": 90},
        ).status_code == 200
        alert = _alert("CRITICAL") | {"status_information": "Network connection timeout"}
        assert client.post("/api/v1/ingest", json=_snapshot([alert])).status_code == 202
        assert client.post("/api/v1/ingest", json=_snapshot([])).status_code == 202

    with connect(app.state.settings.database_path) as db:
        deliveries = db.execute(
            "SELECT e.event_type, e.payload_json FROM notification_deliveries d JOIN notification_events e ON e.id=d.event_id",
        ).fetchall()
        assert len(deliveries) == 1
        assert deliveries[0]["event_type"] == "recovered"
        assert json.loads(deliveries[0]["payload_json"])["status_information"] == "Network connection timeout"


def test_ingest_transaction_outbox_retries_partial_and_recovery(app_factory, key_file, public_dns):
    app = app_factory()
    sent = []
    outcomes = [
        DeliveryResult(False, True, "Provider rate limited the request (HTTP 429)", 429, 0),
        DeliveryResult(True, False, status_code=202),
    ]

    async def fake_send(provider, config, secrets, context, **kwargs):
        sent.append((provider, config, secrets, context, kwargs))
        return outcomes.pop(0) if outcomes else DeliveryResult(True, False, status_code=200)

    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        assert client.post("/api/v1/notifications/rules", json=_rule_payload(channel["id"])).status_code == 201
        assert client.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 90}).status_code == 200

        assert client.post("/api/v1/ingest", json=_snapshot([_alert()])).status_code == 202
        with connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM notification_events WHERE event_type='opened'").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 1
            first_episode = db.execute("SELECT episode_id FROM notification_events WHERE event_type='opened'").fetchone()[0]

        # An identical full snapshot must not duplicate events or outbox work.
        assert client.post("/api/v1/ingest", json=_snapshot([_alert()])).status_code == 202
        with connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 1

        worker = NotificationWorker(app.state.settings.database_path, sender=fake_send, poll_seconds=0.05)
        assert _run(worker.process_one()) is True
        with connect(app.state.settings.database_path) as db:
            pending = db.execute("SELECT status, attempt_count FROM notification_deliveries").fetchone()
            assert tuple(pending) == ("pending", 1)
        assert _run(worker.process_one()) is True
        with connect(app.state.settings.database_path) as db:
            assert tuple(db.execute("SELECT status, attempt_count FROM notification_deliveries").fetchone()) == ("sent", 2)
            assert [row[0] for row in db.execute("SELECT outcome FROM notification_attempts ORDER BY attempt_number")] == ["retry", "sent"]
        assert sent[0][2]["headers"]["X-Lab-Secret"] == "secret-value-never-returned"

        # Non-OK server snapshots keep the previous alert and do not infer recovery.
        error_snapshot = _snapshot([], status="error", complete=False, timestamp="2026-10-14T22:01:00Z")
        assert client.post("/api/v1/ingest", json=error_snapshot).status_code == 202
        with connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM active_alerts").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM notification_events WHERE event_type='recovered'").fetchone()[0] == 0

        # An OK server is reconciled even when another server makes this
        # snapshot partial; completeness only controls removal of omitted servers.
        partial_ok_snapshot = {
            "timestamp": "2026-10-14T22:02:00Z", "source": "NaftaMon", "complete": False,
            "servers": [
                {"server": "Thruk oficina", "status": "ok"},
                {"server": "Thruk remoto", "status": "error"},
            ],
            "alerts": [],
        }
        assert client.post("/api/v1/ingest", json=partial_ok_snapshot).status_code == 202
        with connect(app.state.settings.database_path) as db:
            assert db.execute("SELECT COUNT(*) FROM active_alerts").fetchone()[0] == 0
            assert db.execute("SELECT COUNT(*) FROM notification_events WHERE event_type='recovered'").fetchone()[0] == 1

        # A later opening is a fresh episode and creates a new event ID.
        assert client.post("/api/v1/ingest", json=_snapshot([_alert()], timestamp="2026-10-14T22:03:00Z")).status_code == 202
        with connect(app.state.settings.database_path) as db:
            episodes = [row[0] for row in db.execute("SELECT episode_id FROM notification_events WHERE event_type='opened' ORDER BY rowid")]
            assert len(episodes) == 2
            assert episodes[0] == first_episode
            assert episodes[1] != first_episode


def test_disabling_engine_keeps_outbox_pending(app_factory, key_file, public_dns):
    app = app_factory()
    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        client.post("/api/v1/notifications/rules", json=_rule_payload(channel["id"]))
        client.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 90})
        client.post("/api/v1/ingest", json=_snapshot([_alert()]))
        client.put("/api/v1/notifications/settings", json={"enabled": False, "history_retention_days": 90})

    worker = NotificationWorker(app.state.settings.database_path)
    assert _run(worker.process_one()) is False
    with connect(app.state.settings.database_path) as db:
        assert db.execute("SELECT status FROM notification_deliveries").fetchone()[0] == "cancelled"


def test_reminders_are_queued_only_for_fresh_active_alerts(app_factory, key_file, public_dns):
    app = app_factory()

    async def fake_send(*args, **kwargs):
        return DeliveryResult(True, False, status_code=200)

    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        assert client.post("/api/v1/notifications/rules", json=_rule_payload(channel["id"])).status_code == 201
        client.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 90})
        client.post("/api/v1/ingest", json=_snapshot([_alert()]))

    worker = NotificationWorker(app.state.settings.database_path, sender=fake_send)
    assert _run(worker.process_one()) is True
    now = datetime.now(timezone.utc)
    assert worker.schedule_due_reminders(now=now + timedelta(seconds=3)) == 1
    with connect(app.state.settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM notification_events WHERE event_type='reminder'").fetchone()[0] == 1
        db.execute(
            "UPDATE notification_suppression_state SET next_reminder_at=?",
            ((now - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),),
        )

    # A valid alert whose source has gone stale stays active, but gets no reminder.
    assert worker.schedule_due_reminders(now=now + timedelta(hours=2)) == 0
    with connect(app.state.settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM notification_events WHERE event_type='reminder'").fetchone()[0] == 1


def test_reminders_are_scoped_to_due_rule_and_have_distinct_event_revisions(app_factory, key_file, public_dns):
    app = app_factory()

    async def fake_send(*args, **kwargs):
        return DeliveryResult(True, False, status_code=200)

    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        first_rule = client.post(
            "/api/v1/notifications/rules",
            json=_rule_payload(channel["id"], name="fast reminders", reminder_interval_seconds=1),
        ).json()
        second_rule = client.post(
            "/api/v1/notifications/rules",
            json=_rule_payload(channel["id"], name="slow reminders", reminder_interval_seconds=100),
        ).json()
        client.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 90})
        client.post("/api/v1/ingest", json=_snapshot([_alert()]))

    worker = NotificationWorker(app.state.settings.database_path, sender=fake_send)
    assert _run(worker.process_one()) is True
    assert _run(worker.process_one()) is True
    now = datetime.now(timezone.utc)

    # Only the rule whose own interval has elapsed receives the first reminder.
    assert worker.schedule_due_reminders(now=now + timedelta(seconds=3)) == 1
    with connect(app.state.settings.database_path) as db:
        first = db.execute(
            """SELECT r.name FROM notification_deliveries d
               JOIN notification_events e ON e.id=d.event_id
               JOIN notification_rules r ON r.id=d.rule_id
               WHERE e.event_type='reminder'"""
        ).fetchall()
        assert [row[0] for row in first] == ["fast reminders"]
        assert db.execute(
            "UPDATE notification_suppression_state SET next_reminder_at=? WHERE rule_id=?",
            ((now + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), second_rule["id"]),
        ).rowcount == 1

    # When the slower rule becomes due, the fast rule is not re-enqueued before
    # its next interval and the event revision remains unique per episode.
    assert worker.schedule_due_reminders(now=now + timedelta(seconds=3)) == 1
    with connect(app.state.settings.database_path) as db:
        rows = db.execute(
            """SELECT r.name, e.revision FROM notification_deliveries d
               JOIN notification_events e ON e.id=d.event_id
               JOIN notification_rules r ON r.id=d.rule_id
               WHERE e.event_type='reminder' ORDER BY r.name"""
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [("fast reminders", 1), ("slow reminders", 2)]
        assert first_rule["id"] != second_rule["id"]
        assert db.execute("SELECT COUNT(*) FROM notification_deliveries WHERE status='pending'").fetchone()[0] == 2


def test_provider_http_contracts_and_status_classification(public_dns):
    cases = [
        ("telegram", {"chat_id": "42"}, {"bot_token": "12345:" + "A" * 24}, "api.telegram.org", "/bot"),
        ("gotify", {"base_url": "https://gotify.example.test", "priority": 5}, {"app_token": "gotify-secret"}, "gotify.example.test", "/message"),
        ("ntfy", {"base_url": "https://ntfy.example.test", "topic": "alerts", "priority": 3}, {"access_token": "ntfy-secret"}, "ntfy.example.test", "/"),
        ("webhook", {"url": "https://sink.example.test/hook", "header_names": ["X-Secret"]}, {"headers": {"X-Secret": "hook-secret"}}, "sink.example.test", "/hook"),
    ]
    for provider, config, secrets, host, path_prefix in cases:
        observed = []

        async def handler(request):
            observed.append(request)
            return httpx.Response(429, headers={"Retry-After": "7"})

        result = _run(send_notification(
            provider, config, secrets,
            {"event_type": "opened", "host": "db01", "service": "disk", "state": "CRITICAL"},
            subject_template="Alert {host}", body_template="{event_type} {state}",
            transport=httpx.MockTransport(handler),
        ))
        assert result.transient is True
        assert result.status_code == 429
        assert result.retry_after_seconds == 7
        request = observed[0]
        assert request.method == "POST"
        assert request.url.host == host
        assert request.url.path.startswith(path_prefix)
        if provider == "gotify":
            assert request.headers["X-Gotify-Key"] == "gotify-secret"
            assert "gotify-secret" not in str(request.url)
        if provider == "ntfy":
            assert request.url.path == "/"
            assert json.loads(request.content)["topic"] == "alerts"
        if provider == "webhook":
            assert request.headers["X-Secret"] == "hook-secret"
        if provider == "telegram":
            assert json.loads(request.content)["chat_id"] == "42"


def test_provider_500_timeout_connection_and_dns_pinning(public_dns, monkeypatch):
    context = {"event_type": "opened", "host": "db01", "state": "CRITICAL"}
    args = (
        "webhook", {"url": "https://sink.example.test/hook", "header_names": []}, {}, context,
    )
    kwargs = {"subject_template": "{event_type}", "body_template": "{host}"}

    async def server_error(request):
        return httpx.Response(500)
    result = _run(send_notification(*args, **kwargs, transport=httpx.MockTransport(server_error)))
    assert result.transient and result.status_code == 500

    async def timeout(request):
        raise httpx.ReadTimeout("private diagnostic text", request=request)
    result = _run(send_notification(*args, **kwargs, transport=httpx.MockTransport(timeout)))
    assert result.transient is True
    assert result.error == "Provider request timed out"
    assert "private diagnostic" not in result.error

    async def connection_error(request):
        raise httpx.ConnectError("private diagnostic text", request=request)
    result = _run(send_notification(*args, **kwargs, transport=httpx.MockTransport(connection_error)))
    assert result.transient is True
    assert result.error == "Provider connection failed"

    destination = prepare_destination_url("https://sink.example.test/hook")
    seen = []

    async def capture(request):
        seen.append(request)
        return httpx.Response(204)
    transport = PinnedAddressTransport(destination, httpx.MockTransport(capture))

    async def pinned_request():
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            return await client.post(destination.url, json={"ok": True})
    assert _run(pinned_request()).status_code == 204
    assert seen[0].url.host == "93.184.216.34"
    assert seen[0].headers["host"] == "sink.example.test"
    assert seen[0].extensions["sni_hostname"] == "sink.example.test"


def test_private_dns_targets_are_rejected_but_explicit_ip_literal_can_be_allowed(monkeypatch):
    from app.notifications import security

    monkeypatch.setenv("NOTIFICATION_ALLOWED_DESTINATION_CIDRS", "10.0.0.0/8")
    monkeypatch.setattr(security, "resolve_host", lambda host: {ipaddress.ip_address("10.1.2.3")})
    with pytest.raises(DestinationRejected):
        prepare_destination_url("http://gotify.lan:8080")
    allowed = validate_destination_url("http://10.1.2.3:8080", allowed_networks=(ipaddress.ip_network("10.0.0.0/8"),))
    assert allowed == "http://10.1.2.3:8080"
    with pytest.raises(DestinationRejected):
        validate_destination_url("http://127.0.0.1:9000")


def test_v3_migration_preserves_alert_without_backfilling_notification_events(tmp_path):
    path = tmp_path / "legacy-v3.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY)")
    for version in range(1, 4):
        for statement in MIGRATIONS[version]:
            db.execute(statement)
        db.execute("INSERT INTO schema_version(version) VALUES (?)", (version,))

    timestamp = "2026-10-14T22:00:00.000Z"
    cursor = db.execute(
        """INSERT INTO sources(source_name, peer_ip, first_received_at, last_received_at,
           payload_timestamp, complete) VALUES ('NaftaMon', '192.168.1.20', ?, ?, ?, 1)""",
        (timestamp, timestamp, timestamp),
    )
    source_id = cursor.lastrowid
    payload = json.dumps({"servers": [{"server": "Thruk oficina", "status": "ok"}]})
    db.execute(
        """INSERT INTO latest_snapshots(source_id, payload_json, content_hash, payload_timestamp, received_at, complete)
           VALUES (?, ?, 'hash', ?, ?, 1)""",
        (source_id, payload, timestamp, timestamp),
    )
    db.execute(
        """INSERT INTO monitored_servers(source_id, name, status, status_received_at, last_ok_at, removed)
           VALUES (?, 'Thruk oficina', 'ok', ?, ?, 0)""",
        (source_id, timestamp, timestamp),
    )
    db.execute(
        """INSERT INTO active_alerts(source_id, server, backend, host, service, type, state, hard,
           acknowledged, downtime, last_check, last_change, first_seen_at, last_seen_at)
           VALUES (?, 'Thruk oficina', 'alpha', 'db01', 'disk', 'service', 'CRITICAL', 1, 0, 0, ?, ?, ?, ?)""",
        (source_id, timestamp, timestamp, timestamp, timestamp),
    )
    db.commit()
    db.close()

    initialize(path)
    with connect(path) as migrated:
        alert = migrated.execute(
            "SELECT episode_id, notification_revision, status_information_available FROM active_alerts",
        ).fetchone()
        assert (alert["episode_id"], alert["notification_revision"]) == ("legacy-1", 0)
        assert alert["status_information_available"] == 0
        assert migrated.execute("SELECT enabled FROM notification_settings WHERE id=1").fetchone()[0] == 0
        assert migrated.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0] == 0


def test_expired_worker_lease_is_recovered_after_restart(app_factory, key_file, public_dns):
    app = app_factory()
    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        client.post("/api/v1/notifications/rules", json=_rule_payload(channel["id"]))
        client.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 90})
        assert client.post("/api/v1/ingest", json=_snapshot([_alert()])).status_code == 202

    # Simulate a process dying after it claimed the row but before recording an
    # outcome. A fresh worker instance must reclaim the persisted outbox row.
    with connect(app.state.settings.database_path) as db:
        db.execute(
            """UPDATE notification_deliveries SET status='in_flight', attempt_count=1,
               lease_until='2000-01-01T00:00:00.000Z', next_attempt_at='2000-01-01T00:00:00.000Z'"""
        )

    calls = []

    async def fake_send(*args, **kwargs):
        calls.append(kwargs["delivery_id"])
        return DeliveryResult(True, False, status_code=202)

    restarted = NotificationWorker(app.state.settings.database_path, sender=fake_send)
    assert _run(restarted.process_one()) is True
    assert len(calls) == 1
    with connect(app.state.settings.database_path) as db:
        row = db.execute("SELECT status, attempt_count FROM notification_deliveries").fetchone()
        assert tuple(row) == ("sent", 2)
        assert db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0] == 1


def test_cooldown_suppresses_changes_but_new_episode_can_notify(app_factory, key_file, public_dns):
    app = app_factory()
    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel_payload()).json()
        rule = _rule_payload(channel["id"], cooldown_seconds=3600, reminder_interval_seconds=None)
        assert client.post("/api/v1/notifications/rules", json=rule).status_code == 201
        client.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 90})
        assert client.post("/api/v1/ingest", json=_snapshot([_alert()])).status_code == 202

    async def fake_send(*args, **kwargs):
        return DeliveryResult(True, False, status_code=202)

    worker = NotificationWorker(app.state.settings.database_path, sender=fake_send)
    assert _run(worker.process_one()) is True
    with TestClient(app) as client:
        assert client.post("/api/v1/ingest", json=_snapshot([_alert("WARNING")])).status_code == 202
        assert client.post("/api/v1/ingest", json=_snapshot([])).status_code == 202
        assert client.post("/api/v1/ingest", json=_snapshot([_alert("CRITICAL")])).status_code == 202
    with connect(app.state.settings.database_path) as db:
        events = db.execute("SELECT event_type, episode_id FROM notification_events ORDER BY occurred_at, rowid").fetchall()
        assert [row["event_type"] for row in events] == ["opened", "state_changed", "recovered", "opened"]
        assert events[0]["episode_id"] != events[-1]["episode_id"]
        deliveries = db.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
        assert deliveries == 2, "Only the first and reopened episode should be queued"
