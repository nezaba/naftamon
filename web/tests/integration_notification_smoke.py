"""End-to-end Docker check against web, worker and internal mock-provider.

Run in an isolated Compose project with --profile mock --profile test after
creating its own notification key. This script uses only invented alert data.
"""

from datetime import datetime, timezone
import socket
import time
from uuid import uuid4

import httpx


API = "http://web:8000"
MOCK = "http://mock-provider:8765"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def require(response: httpx.Response, expected: int) -> dict:
    assert response.status_code == expected, (response.status_code, response.text[:500])
    return response.json()


def wait_for(predicate, timeout: float = 12.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.25)
    raise AssertionError("Timed out waiting for the notification worker")


def main() -> None:
    suffix = uuid4().hex[:8]
    host = f"lab-host-{suffix}"
    server = f"lab-thruk-{suffix}"
    ip = socket.gethostbyname("mock-provider")
    with httpx.Client(timeout=5) as client:
        assert require(client.get(f"{API}/healthz"), 200)["status"] == "ok"
        channel = require(client.post(f"{API}/api/v1/notifications/channels", json={
            "name": f"lab-webhook-{suffix}", "provider": "webhook", "enabled": True,
            "config": {
                "url": f"http://{ip}:8765/hook", "header_names": ["X-Lab-Test"],
                "json_template": {"event": "{event_type}", "host": "{host}", "state": "{state}"},
            },
            "secrets": {"headers": {"X-Lab-Test": "fake-lab-token"}},
            "timeout_seconds": 3, "rate_limit_seconds": 0,
        }), 201)
        assert channel["secret_configured"] is True
        assert "fake-lab-token" not in str(channel)
        rule = require(client.post(f"{API}/api/v1/notifications/rules", json={
            "name": f"lab-rule-{suffix}", "enabled": True,
            "conditions": {"operator": "AND", "conditions": [
                {"field": "host", "operator": "equals", "value": host},
            ]},
            "events": ["opened", "state_changed", "recovered"],
            "channel_ids": [channel["id"]],
        }), 201)
        require(client.put(f"{API}/api/v1/notifications/settings", json={
            "enabled": True, "history_retention_days": 90,
        }), 200)

        def received_events() -> list[str]:
            items = require(client.get(f"{MOCK}/received"), 200)["items"]
            return [item["body"]["event"] for item in items if item["body"].get("host") == host]

        def snapshot(alerts: list[dict], status: str = "ok", complete: bool = True) -> dict:
            return {
                "timestamp": utc_now(), "source": "NaftaMon", "complete": complete,
                "servers": [{"server": server, "status": status}], "alerts": alerts,
            }

        alert = {
            "server": server, "backend": "lab", "host": host, "service": "disk",
            "type": "service", "state": "CRITICAL", "hard": True,
            "acknowledged": False, "downtime": False,
            "last_check": utc_now(), "last_change": utc_now(),
        }
        first = snapshot([alert])
        require(client.post(f"{API}/api/v1/ingest", json=first), 202)
        wait_for(lambda: received_events() == ["opened"])
        require(client.post(f"{API}/api/v1/ingest", json=first), 202)
        time.sleep(1.5)
        assert received_events() == ["opened"], "Identical snapshot produced a duplicate"
        require(client.post(f"{API}/api/v1/ingest", json=snapshot([], "error", False)), 202)
        time.sleep(1.5)
        assert received_events() == ["opened"], "Thruk error produced a recovery"
        require(client.post(f"{API}/api/v1/ingest", json=snapshot([])), 202)
        wait_for(lambda: received_events() == ["opened", "recovered"])

        history = require(client.get(f"{API}/api/v1/notifications/history", params={
            "rule_id": rule["id"], "limit": 100,
        }), 200)
        assert history["total"] == 2, history
        assert all(item["status"] == "sent" for item in history["items"]), history
        dry_run = require(client.post(f"{API}/api/v1/notifications/rules/{rule['id']}/test"), 200)
        assert dry_run["dry_run"] is True and dry_run["sent"] is False
        assert received_events() == ["opened", "recovered"], "Rule test sent a message"
        explicit = require(client.post(f"{API}/api/v1/notifications/channels/{channel['id']}/test"), 200)
        assert explicit["ok"] is True

        gotify = require(client.post(f"{API}/api/v1/notifications/channels", json={
            "name": f"lab-gotify-{suffix}", "provider": "gotify", "enabled": True,
            "config": {"base_url": f"http://{ip}:8765", "priority": 5},
            "secrets": {"app_token": "fake-gotify-token"},
            "timeout_seconds": 3, "rate_limit_seconds": 0,
        }), 201)
        require(client.post(f"{API}/api/v1/notifications/channels/{gotify['id']}/test"), 200)
        ntfy = require(client.post(f"{API}/api/v1/notifications/channels", json={
            "name": f"lab-ntfy-{suffix}", "provider": "ntfy", "enabled": True,
            "config": {"base_url": f"http://{ip}:8765", "topic": f"lab_{suffix}", "priority": 3},
            "secrets": {"access_token": "fake-ntfy-token"},
            "timeout_seconds": 3, "rate_limit_seconds": 0,
        }), 201)
        require(client.post(f"{API}/api/v1/notifications/channels/{ntfy['id']}/test"), 200)
        received = require(client.get(f"{MOCK}/received"), 200)["items"]
        assert any(item["path"] == "/message" and item["body"]["priority"] == 5 for item in received)
        assert any(item["path"] == "/" and item["body"]["topic"] == f"lab_{suffix}" for item in received)

        for code, suffix_path in ((500, "500"), (429, "429")):
            failing = require(client.post(f"{API}/api/v1/notifications/channels", json={
                "name": f"lab-failure-{suffix_path}-{suffix}", "provider": "webhook", "enabled": True,
                "config": {"url": f"http://{ip}:8765/fail/{suffix_path}"},
                "timeout_seconds": 3, "rate_limit_seconds": 0,
            }), 201)
            response = client.post(f"{API}/api/v1/notifications/channels/{failing['id']}/test")
            result = require(response, 502)
            assert result["ok"] is False and result["status_code"] == code
            if code == 429:
                assert result["retry_after_seconds"] == 1
        print("PASS: HTTP ingest/outbox/worker and Webhook, Gotify, ntfy mocks; dedupe, Thruk error, recovery, dry-run, 500/429")


if __name__ == "__main__":
    main()
