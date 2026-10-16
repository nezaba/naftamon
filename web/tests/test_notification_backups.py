import json
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.db import connect
from app.main import create_app
from app.notifications.crypto import decrypt_secrets


@pytest.fixture
def backup_key_file(tmp_path, monkeypatch):
    path = tmp_path / "notification.key"
    path.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("NOTIFICATION_KEY_FILE", str(path))
    return path


@pytest.fixture
def backup_public_dns(monkeypatch):
    import ipaddress
    from app.notifications import security

    monkeypatch.setattr(security, "resolve_host", lambda host: {ipaddress.ip_address("93.184.216.34")})


def _channel(name, provider="webhook", *, secret=True):
    if provider == "gotify":
        return {
            "name": name, "provider": provider,
            "config": {"base_url": "https://gotify.example.test", "priority": 4},
            "secrets": {"app_token": "fake-gotify-token"} if secret else None,
            "enabled": True, "timeout_seconds": 5, "rate_limit_seconds": 2,
        }
    return {
        "name": name, "provider": "webhook",
        "config": {
            "url": "https://sink.example.test/hook",
            "header_names": ["X-Backup-Secret"] if secret else [],
            "json_template": {"message": "{host} {state}"},
        },
        "secrets": {"headers": {"X-Backup-Secret": "never-export-this-secret"}} if secret else None,
        "enabled": True, "timeout_seconds": 7, "rate_limit_seconds": 3,
    }


def _rule(channel_ids, name="Status rule"):
    return {
        "name": name, "description": "Rule with Status Information",
        "enabled": True,
        "conditions": {"operator": "AND", "conditions": [
            {"field": "state", "operator": "equals", "value": "CRITICAL"},
            {"field": "status_information", "operator": "contains", "value": "DISK CRITICAL"},
        ]},
        "events": ["opened", "state_changed"], "channel_ids": channel_ids,
        "cooldown_seconds": 45, "reminder_interval_seconds": None,
        "subject_template": "{event_type} {host}", "body_template": "{state}",
    }


def _alert_snapshot():
    now = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return {
        "timestamp": now, "source": "NaftaMon", "complete": True,
        "servers": [{"server": "Backup lab", "status": "ok"}],
        "alerts": [{
            "server": "Backup lab", "backend": "alpha", "host": "db01", "service": "disk",
            "type": "service", "state": "CRITICAL", "hard": True,
            "acknowledged": False, "downtime": False,
            "last_check": now, "last_change": now, "status_information": "DISK CRITICAL threshold exceeded",
        }],
    }


def _app(app_factory, path=None):
    return app_factory(database_path=path) if path else app_factory()


def test_encrypted_backup_restores_settings_rules_associations_and_secrets_between_databases(
    app_factory, backup_key_file, backup_public_dns, monkeypatch, tmp_path,
):
    source_app = _app(app_factory, tmp_path / "source.sqlite3")
    password = "A-strong-lab-passphrase-2026"
    with TestClient(source_app) as source:
        webhook = source.post("/api/v1/notifications/channels", json=_channel("Ops webhook")).json()
        gotify = source.post("/api/v1/notifications/channels", json=_channel("Chat alerts", "gotify")).json()
        assert webhook["secret_configured"] is True and gotify["secret_configured"] is True
        source.post("/api/v1/notifications/rules", json=_rule([webhook["id"], gotify["id"]]))
        source.post("/api/v1/notifications/rules", json=_rule([], name="Second rule"))
        source.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 45})

        exported = source.post("/api/v1/notifications/backup/export", json={"password": password})
        assert exported.status_code == 200
        document = exported.json()
        assert "never-export-this-secret" not in exported.text
        assert "fake-gotify-token" not in exported.text
        assert "secrets_encrypted" in document
        assert document["notifications"]["rules"][0]["conditions"]["conditions"][1]["field"] == "status_information"
        assert document["notifications"]["rules"][0]["channel_keys"] == ["channel-0001", "channel-0002"]

    # Restore under a distinct application encryption key; encrypted backup
    # secrets are re-encrypted with the target installation's key.
    target_key = tmp_path / "target-notification.key"
    target_key.write_bytes(Fernet.generate_key())
    monkeypatch.setenv("NOTIFICATION_KEY_FILE", str(target_key))
    target_app = _app(app_factory, tmp_path / "target.sqlite3")
    with TestClient(target_app) as target:
        assert target.post("/api/v1/ingest", json=_alert_snapshot()).status_code == 202
        with connect(target_app.state.settings.database_path) as db:
            history_before_restore = db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0]
            deliveries_before_restore = db.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0]
        preview = target.post("/api/v1/notifications/backup/preview", json={"backup": document, "password": password})
        assert preview.status_code == 200
        assert (preview.json()["rules"], preview.json()["channels"]) == (2, 2)
        assert preview.json()["enabled_rules"] == 2
        restored = target.post(
            "/api/v1/notifications/backup/restore",
            json={"backup": document, "password": password, "confirm_replace": True},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["restored"] is True
        settings = target.get("/api/v1/notifications/settings").json()
        assert settings["enabled"] is True and settings["history_retention_days"] == 45
        channels = target.get("/api/v1/notifications/channels").json()["items"]
        rules = target.get("/api/v1/notifications/rules").json()["items"]
        assert len(channels) == 2 and len(rules) == 2
        channel_by_name = {item["name"]: item for item in channels}
        rule_by_name = {item["name"]: item for item in rules}
        restored_rule = rule_by_name["Status rule"]
        assert {channel_by_name[name]["id"] for name in ("Ops webhook", "Chat alerts")} == set(restored_rule["channel_ids"])
        assert restored_rule["conditions"]["conditions"][1]["field"] == "status_information"
        dry_run = target.post(f"/api/v1/notifications/rules/{restored_rule['id']}/test")
        assert dry_run.status_code == 200 and dry_run.json()["match_count"] == 1
        with connect(target_app.state.settings.database_path) as db:
            rows = db.execute("SELECT name, secrets_ciphertext FROM notification_channels ORDER BY name").fetchall()
            secret_map = {row["name"]: decrypt_secrets(row["secrets_ciphertext"]) for row in rows}
            assert secret_map["Ops webhook"]["headers"]["X-Backup-Secret"] == "never-export-this-secret"
            assert secret_map["Chat alerts"]["app_token"] == "fake-gotify-token"
            assert db.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0] == history_before_restore
            assert db.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == deliveries_before_restore


def test_plain_backup_excludes_secrets_warns_and_disables_affected_channels(
    app_factory, backup_key_file, backup_public_dns,
):
    app = _app(app_factory)
    with TestClient(app) as client:
        channel = client.post("/api/v1/notifications/channels", json=_channel("Private hook")).json()
        document_response = client.post("/api/v1/notifications/backup/export", json={"password": None})
        assert document_response.status_code == 200
        document = document_response.json()
        assert "never-export-this-secret" not in document_response.text
        assert "secrets_encrypted" not in document
        assert document["notifications"]["channels"][0]["secret_configured"] is True
        assert document["notifications"]["channels"][0]["enabled"] is True

        preview = client.post("/api/v1/notifications/backup/preview", json={"backup": document, "password": None})
        assert preview.status_code == 200
        assert "deshabilitado" in preview.json()["warnings"][0]
        result = client.post(
            "/api/v1/notifications/backup/restore",
            json={"backup": document, "password": None, "confirm_replace": True},
        )
        assert result.status_code == 200
        channel_after = client.get("/api/v1/notifications/channels").json()["items"][0]
        assert channel_after["enabled"] is False and channel_after["secret_configured"] is False
        assert channel_after["name"] == channel["name"]


def test_empty_configuration_round_trip_and_bad_files(app_factory, backup_key_file):
    app = _app(app_factory)
    with TestClient(app) as client:
        assert client.get("/api/v1/notifications/backup/previous").json() == {"available": False}
        empty = client.post("/api/v1/notifications/backup/export", json={"password": None}).json()
        assert empty["notifications"]["channels"] == []
        assert empty["notifications"]["rules"] == []
        assert "alerts" not in empty and "sources" not in empty and "history" not in empty
        assert client.post("/api/v1/notifications/backup/restore", json={
            "backup": empty, "password": None, "confirm_replace": True,
        }).status_code == 200
        assert client.post("/api/v1/notifications/backup/preview", content="not-json", headers={"content-type": "application/json"}).status_code == 400
        assert client.post(
            "/api/v1/notifications/backup/preview",
            content=b'{"backup":{},"backup":{},"password":null}',
            headers={"content-type": "application/json"},
        ).status_code == 400
        oversized = client.post(
            "/api/v1/notifications/backup/preview",
            content=b" " * (2 * 1024 * 1024 + 9000),
            headers={"content-type": "application/json"},
        )
        assert oversized.status_code == 413
        wrong_format = dict(empty, format="another-app")
        assert client.post("/api/v1/notifications/backup/preview", json={"backup": wrong_format, "password": None}).status_code == 422
        wrong_version = dict(empty, schema_version=999)
        assert client.post("/api/v1/notifications/backup/preview", json={"backup": wrong_version, "password": None}).status_code == 422
        assert client.post("/api/v1/notifications/backup/preview", json={"backup": empty, "password": "unexpected"}).status_code == 422
        no_confirmation = client.post("/api/v1/notifications/backup/restore", json={
            "backup": empty, "password": None, "confirm_replace": False,
        })
        assert no_confirmation.status_code == 409


def test_restore_rejects_dangling_channel_references_and_wrong_encryption_password(app_factory, backup_key_file):
    app = _app(app_factory)
    with TestClient(app) as client:
        encrypted = client.post("/api/v1/notifications/backup/export", json={"password": "A-strong-lab-passphrase-2026"}).json()
        assert client.post("/api/v1/notifications/backup/preview", json={
            "backup": encrypted, "password": "a-different-passphrase-2026",
        }).status_code == 422
        invalid = json.loads(json.dumps(encrypted))
        invalid["notifications"]["rules"] = [{
            "name": "dangling", "description": "", "enabled": False,
            "conditions": {"operator": "AND", "conditions": [{"field": "status_information", "operator": "contains", "value": "down"}]},
            "events": ["opened"], "channel_keys": ["missing-channel"], "cooldown_seconds": 0,
            "reminder_interval_seconds": None, "subject_template": "{host}", "body_template": "{state}",
        }]
        preview = client.post("/api/v1/notifications/backup/preview", json={
            "backup": invalid, "password": "A-strong-lab-passphrase-2026",
        })
        assert preview.status_code == 422


def test_sql_failure_rolls_back_entire_replacement_and_keeps_old_configuration(app_factory, backup_key_file, backup_public_dns):
    app = _app(app_factory)
    with TestClient(app) as client:
        old_channel = client.post("/api/v1/notifications/channels", json=_channel("Existing hook")).json()
        client.post("/api/v1/notifications/rules", json=_rule([old_channel["id"]], name="Existing rule"))
        client.put("/api/v1/notifications/settings", json={"enabled": True, "history_retention_days": 77})
        backup = client.post("/api/v1/notifications/backup/export", json={"password": None}).json()
        backup["notifications"]["rules"][0]["name"] = "Replacement that will fail"
        with connect(app.state.settings.database_path) as db:
            db.execute("""CREATE TRIGGER fail_replacement_rule BEFORE INSERT ON notification_rules
                          BEGIN SELECT RAISE(ABORT, 'forced test rollback'); END""")

        result = client.post("/api/v1/notifications/backup/restore", json={
            "backup": backup, "password": None, "confirm_replace": True,
        })
        assert result.status_code == 500
        assert "forced test rollback" not in result.text
        assert client.get("/api/v1/notifications/settings").json()["history_retention_days"] == 77
        assert [item["name"] for item in client.get("/api/v1/notifications/channels").json()["items"]] == ["Existing hook"]
        assert [item["name"] for item in client.get("/api/v1/notifications/rules").json()["items"]] == ["Existing rule"]


def test_restore_previous_recovers_credentials_and_keeps_a_new_rollback_point(app_factory, backup_key_file, backup_public_dns):
    app = _app(app_factory)
    with TestClient(app) as client:
        original = client.post("/api/v1/notifications/channels", json=_channel("Original hook")).json()
        client.post("/api/v1/notifications/rules", json=_rule([original["id"]], name="Original rule"))
        current_export = client.post("/api/v1/notifications/backup/export", json={"password": None}).json()
        current_export["notifications"]["channels"][0]["name"] = "Replacement hook"
        current_export["notifications"]["rules"][0]["name"] = "Replacement rule"
        assert client.post("/api/v1/notifications/backup/restore", json={
            "backup": current_export, "password": None, "confirm_replace": True,
        }).status_code == 200

        summary = client.get("/api/v1/notifications/backup/previous").json()
        assert summary["available"] is True and summary["encrypted_secrets"] is True
        assert summary["rules"] == 1 and summary["channels"] == 1
        restored = client.post("/api/v1/notifications/backup/restore-previous", json={"confirm_replace": True})
        assert restored.status_code == 200, restored.text
        channel = client.get("/api/v1/notifications/channels").json()["items"][0]
        rule = client.get("/api/v1/notifications/rules").json()["items"][0]
        assert channel["name"] == "Original hook" and channel["enabled"] is True and channel["secret_configured"] is True
        assert rule["name"] == "Original rule" and rule["channel_ids"] == [channel["id"]]
        with connect(app.state.settings.database_path) as db:
            row = db.execute("SELECT secrets_ciphertext FROM notification_channels WHERE id = ?", (channel["id"],)).fetchone()
            assert decrypt_secrets(row["secrets_ciphertext"])["headers"]["X-Backup-Secret"] == "never-export-this-secret"
            assert db.execute("SELECT COUNT(*) FROM notification_config_backups").fetchone()[0] == 2

