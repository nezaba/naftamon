import re


def test_dashboard_is_the_unauthenticated_home_page_with_local_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Estado de infraestructura" in response.text
    assert 'href="/static/dashboard.css?v=sync-v2-20261016"' in response.text
    assert 'src="/static/dashboard.js?v=sync-v2-20261016"' in response.text
    assert not re.search(r'(?:src|href)=["\']https?://', response.text, re.IGNORECASE)
    assert 'id="login"' not in response.text.lower()
    assert not re.search(r'<form[^>]+(?:login|auth)', response.text, re.IGNORECASE)
    assert 'name="username"' not in response.text.lower()
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.ico">' in response.text
    assert response.headers["cache-control"] == "no-cache"

    favicon = client.get("/favicon.ico")
    assert favicon.status_code == 200
    assert favicon.headers["content-type"].startswith("image/svg+xml")
    assert b"<svg" in favicon.content

    css = client.get("/static/dashboard.css")
    js = client.get("/static/dashboard.js")
    assert css.status_code == js.status_code == 200
    assert css.headers["cache-control"] == js.headers["cache-control"] == "no-cache"
    assert "data-theme=\"light\"" in css.text
    assert "fetchAllStatuses" not in js.text
    assert "function statusQuery(offset, limit, revision = null)" in js.text
    assert "state.virtualChunkSize" in js.text
    assert 'view: state.statusView' in js.text
    assert 'monitoring-view' in response.text
    assert 'requestStatuses(offset, limit, targetRevision)' in js.text
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


def test_source_cards_offer_confirmed_manual_removal_for_inactive_sources(client):
    page = client.get("/").text
    assert "no se eliminan automáticamente" in page
    assert 'id="source-delete-dialog"' in page
    assert 'id="source-delete-accept"' in page
    assert 'id="source-delete-cancel"' in page
    assert "historial de notificaciones se conserva" in page
    assert "POST que ya salió hacia el proveedor no se puede retirar" in page
    assert "vuelve a enviar datos válidos" in page

    script = client.get("/static/dashboard.js").text
    for expected in (
        "inactive_after_seconds", "function sourceIsInactive", "sourceIsInactive(source)",
        'fetch(`/api/v1/sources/${sourceId}`, { method: "DELETE"',
        'if (state.refreshing) { state.refreshPending = true; return; }',
    ):
        assert expected in script

    source = client.post("/api/v1/ingest", json={
        "timestamp": "2026-10-14T22:00:00.000Z", "source": "NaftaMon", "complete": True,
        "servers": [{"server": "Thruk oficina", "status": "ok"}], "alerts": [],
    })
    assert source.status_code == 202
    sources = client.get("/api/v1/sources").json()
    assert sources["inactive_after_seconds"] == 86400
    assert sources["items"][0]["inactive"] is False


def test_monitoring_table_exposes_status_information_and_searches_it_safely(client):
    page = client.get("/").text
    assert "Status Information" in page
    assert '<option value="UP">UP / OK</option>' in page
    assert "Buscar en los datos de monitoreo" in page

    script = client.get("/static/dashboard.js").text
    assert 'query.set("query", filter.text)' in script
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


def test_monitoring_page_size_choices_persist_and_all_mode_uses_filtered_rows(client):
    page = client.get("/").text
    assert 'id="page-size"' in page
    assert 'id="page-size" aria-label="Elementos por página"' in page
    for option in (
        '<option value="25">25</option>', '<option value="50" selected>50</option>',
        '<option value="100">100</option>', '<option value="250">250</option>',
        '<option value="all">Todos</option>',
    ):
        assert option in page
    assert 'id="monitoring-scroll"' in page

    script = client.get("/static/dashboard.js").text
    assert 'storageSet("naftamon-monitor-page-size"' in script
    assert 'storageGet("naftamon-monitor-page-size")' in script
    assert 'const allFiltered = state.statuses' in script
    assert 'const showAll = state.pageSize === "all"' in script
    assert 'const virtualized = showAll && total > 250' in script
    assert 'function renderVirtualRows()' in script
    assert 'if (!sampleRow && state.statuses.length)' in script
    assert 'sampleRow?.getBoundingClientRect().height || 56' in script
    assert 'fetchVirtualChunk(offset)' in script
    assert 'setAttribute("aria-rowcount"' in script
    assert 'setAttribute("aria-rowindex"' in script


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
    assert '["status_information", "Status Information"]' in script.text
    assert 'field.value === "status_information"' in script.text
    for operator_label in ("Contiene", "No contiene", "Es exactamente igual", "No es exactamente igual"):
        assert operator_label in script.text
    assert "no coincide, también con operadores negativos" in response.text


def test_notification_settings_exposes_backup_preview_encryption_and_recovery_controls(client):
    page = client.get("/").text
    for element_id in (
        "notification-backup-title", "export-notification-backup", "export-encrypted-notification-backup",
        "backup-export-password", "backup-export-password-confirm", "notification-backup-file",
        "backup-import-password", "preview-notification-backup", "notification-backup-preview",
        "restore-notification-backup", "restore-previous-notification-backup",
    ):
        assert f'id="{element_id}"' in page
    assert "El archivo JSON no incluye credenciales" in page
    assert "no tiene autenticación" in page
    script = client.get("/static/notifications.js").text
    for endpoint in (
        '`${API}/backup/export`', '"/backup/preview"', '"/backup/restore"',
        '"/backup/previous"', '"/backup/restore-previous"',
    ):
        assert endpoint in script
    assert "JSON.parse(raw)" in script
    assert "preview.warnings" in script
    assert "confirm_replace: true" in script
    assert "innerHTML" not in script


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
