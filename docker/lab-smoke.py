#!/usr/bin/env python3
"""Exercise the lab services over Compose's private network."""

import json
from urllib.request import Request, urlopen


with urlopen("http://thruk:18765/thruk/_stats", timeout=5) as response:
    stats = json.load(response)
assert "full" in stats and "small" in stats and "cmd" in stats, stats
print("Thruk mock: OK")

with urlopen("http://webhooks:8080/health", timeout=5) as response:
    health = json.load(response)
assert health == {"status": "ok"}, health

request = Request(
    "http://webhooks:8080/webhook",
    data=b'{"event":"lab-check"}',
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urlopen(request, timeout=5) as response:
    result = json.load(response)
    assert response.status == 202 and result == {"received": True}, result
print("Webhook receiver: OK (check 'docker compose logs webhooks' for the JSON)")
