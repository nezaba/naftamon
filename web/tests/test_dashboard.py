import re


def test_dashboard_is_the_unauthenticated_home_page_with_local_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Estado de infraestructura" in response.text
    assert 'href="/static/dashboard.css"' in response.text
    assert 'src="/static/dashboard.js"' in response.text
    assert not re.search(r'(?:src|href)=["\']https?://', response.text, re.IGNORECASE)
    assert 'id="login"' not in response.text.lower()
    assert not re.search(r'<form[^>]+(?:login|auth)', response.text, re.IGNORECASE)
    assert 'name="username"' not in response.text.lower()

    css = client.get("/static/dashboard.css")
    js = client.get("/static/dashboard.js")
    assert css.status_code == js.status_code == 200
    assert "data-theme=\"light\"" in css.text
    assert "fetchAllStatuses" in js.text
    assert "/api/v1/statuses?limit=${limit}&offset=${offset}" in js.text
    assert "textContent" in js.text
    assert "innerHTML" not in js.text


def test_dashboard_exposes_real_contract_fields_filters_and_history(client):
    response = client.get("/")
    for real_field in ("backend", "last_seen_at", "UNREACHABLE"):
        assert real_field in response.text
    for filter_id in ("filter-host", "filter-service", "filter-text", "filter-severity", "filter-server", "filter-backend", "filter-source", "filter-ack", "filter-downtime"):
        assert f'id="{filter_id}"' in response.text
    assert 'id="history-view"' in response.text
    script = client.get("/static/dashboard.js").text
    for real_field in ("acknowledged", "downtime", "last_seen_at"):
        assert real_field in script
    assert 'id="sources-grid"' in response.text


def test_monitoring_table_exposes_status_information_and_searches_it_safely(client):
    page = client.get("/").text
    assert "Status Information" in page
    assert '<option value="UP">UP / OK</option>' in page
    assert "Buscar en los datos de monitoreo" in page

    script = client.get("/static/dashboard.js").text
    assert "item.status_information].join" in script
    assert 'statusInformation.trim() ? statusInformation : "-"' in script
    assert "information.title = statusInformation" in script
    assert 'information.setAttribute("aria-label"' in script
    assert "state.statuses = statusResult.items" in script
    assert "textContent" in script
    assert "innerHTML" not in script

    css = client.get("/static/dashboard.css").text
    assert ".status-information-text" in css
    assert "text-overflow: ellipsis" in css
    assert "overflow: hidden" in css
    assert 'data-state="UP"' in css


def test_monitoring_feed_keeps_status_information_attached_to_each_server_and_service(client):
    timestamp = "2026-10-15T20:00:00Z"
    common = {
        "backend": "alpha", "hard": True, "acknowledged": False, "downtime": False,
        "last_check": timestamp, "last_change": timestamp,
    }
    body = {
        "timestamp": timestamp, "source": "NaftaMon", "complete": True,
        "servers": [{"server": "Thruk A", "status": "ok"}, {"server": "Thruk B", "status": "ok"}],
        "alerts": [{
            **common, "server": "Thruk B", "host": "db02", "service": "disk", "type": "service",
            "state": "CRITICAL", "status_information": "DISK CRITICAL — salida del plugin",
        }],
        "ok_statuses": [
            {
                **common, "server": "Thruk A", "host": "web01", "service": "", "type": "host",
                "state": "UP", "status_information": "",
            },
            {
                **common, "server": "Thruk A", "host": "web01", "service": "ssh", "type": "service",
                "state": "UP", "status_information": "SSH OK " + ("detalle largo. " * 40),
            },
        ],
    }
    assert client.post("/api/v1/ingest", json=body).status_code == 202
    response = client.get("/api/v1/statuses?limit=50&offset=0")
    assert response.status_code == 200
    rows = {(item["server"], item["host"], item["service"]): item for item in response.json()["items"]}
    assert rows[("Thruk A", "web01", "")]["status_information"] == ""
    assert rows[("Thruk A", "web01", "ssh")]["status_information"].startswith("SSH OK")
    assert len(rows[("Thruk A", "web01", "ssh")]["status_information"]) > 280
    assert rows[("Thruk B", "db02", "disk")]["status_information"] == "DISK CRITICAL — salida del plugin"


def test_notifications_ui_is_local_secret_safe_and_matches_api_contract(client):
    response = client.get("/")
    assert response.status_code == 200
    for view in ("rules", "channels", "history", "settings"):
        assert f'href="#notifications/{view}"' in response.text
        assert f'data-notification-view="{view}"' in response.text
    assert 'type="password"' in response.text  # Provider credentials, not dashboard login.
    assert "id=\"telegram-bot-token\"" in response.text
    assert "id=\"ntfy-access-token\"" in response.text
    assert 'src="/static/notifications.js"' in response.text
    assert not re.search(r'(?:src|href)=["\']https?://', response.text, re.IGNORECASE)

    script = client.get("/static/notifications.js")
    css = client.get("/static/dashboard.css")
    assert script.status_code == css.status_code == 200
    for path in (
        'apiRequest("/settings")', 'fetchAll("/rules")', 'fetchAll("/channels")',
        '${channel.id}/test', '${rule.id}/duplicate', '/history?',
        'rule_id', 'channel_id', 'params.set("result"', 'params.set("from"',
        'params.set("to"', 'attempt_count', 'last_error', 'dry_run',
    ):
        assert path in script.text
    assert "localStorage" not in script.text
    assert "innerHTML" not in script.text
    assert "notification-history-table" in css.text
    assert "@media (max-width: 540px)" in css.text


def test_notifications_ui_has_no_dangling_static_element_selectors(client):
    import html.parser

    class IdCollector(html.parser.HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = set()

        def handle_starttag(self, tag, attrs):
            for name, value in attrs:
                if name == "id" and value:
                    self.ids.add(value)

    html = client.get("/").text
    collector = IdCollector()
    collector.feed(html)
    script = client.get("/static/notifications.js").text
    fixed_id_selectors = set(re.findall(r'\$\("#([a-zA-Z0-9_-]+)"', script))
    assert fixed_id_selectors <= collector.ids
