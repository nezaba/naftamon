import re


def test_dashboard_is_the_unauthenticated_home_page_with_local_assets(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Estado de infraestructura" in response.text
    assert 'href="/static/dashboard.css"' in response.text
    assert 'src="/static/dashboard.js"' in response.text
    assert not re.search(r'(?:src|href)=["\']https?://', response.text, re.IGNORECASE)
    assert "login" not in response.text.lower()
    assert "password" not in response.text.lower()

    css = client.get("/static/dashboard.css")
    js = client.get("/static/dashboard.js")
    assert css.status_code == js.status_code == 200
    assert "data-theme=\"light\"" in css.text
    assert "fetchAllAlerts" in js.text
    assert "/api/v1/alerts?limit=${limit}&offset=${offset}" in js.text


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
