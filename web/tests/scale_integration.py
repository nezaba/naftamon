"""Synthetic 165,950-status production-shape ingest and API smoke test.

Run from the isolated web Compose test container with NAFTAMON_SCALE_URL set.
The fixture is generated in memory; it never reads production data.
"""

import gzip
import hashlib
import json
import os
import resource
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen


OK_COUNT = 165_950
ALERT_COUNTS = {"CRITICAL": 80, "WARNING": 40, "UNKNOWN": 14, "DOWN": 8, "UNREACHABLE": 6}
SERVER = "Thruk Scale Fixture"
BACKEND = "scale-lab"
LAST_CHECK = "2026-10-16T12:34:50Z"
LAST_CHANGE = "2026-10-14T08:15:30Z"
STATUS_INFORMATION = "OK: all checks pass"


def compact(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def make_statuses() -> bytes:
    records = []
    for index in range(OK_COUNT):
        is_host = index % 4 == 0
        records.append(compact({
            "server": SERVER, "backend": BACKEND, "host": f"node-{index:06d}.corp.example",
            "service": "" if is_host else f"check-{index % 37:02d}",
            "type": "host" if is_host else "service", "state": "UP", "hard": True,
            "acknowledged": False, "downtime": False, "last_check": LAST_CHECK,
            "last_change": LAST_CHANGE, "status_information": STATUS_INFORMATION,
        }))
    return b"[" + b",".join(records) + b"]"


def make_alerts() -> bytes:
    records = []
    index = 0
    for state, amount in ALERT_COUNTS.items():
        for _ in range(amount):
            is_host = state in ("DOWN", "UNREACHABLE")
            records.append(compact({
                "server": SERVER, "backend": BACKEND, "host": f"alert-{index:03d}.corp.example",
                "service": "" if is_host else f"check-{index % 11:02d}",
                "type": "host" if is_host else "service", "state": state, "hard": True,
                "acknowledged": index % 7 == 0, "downtime": index % 13 == 0,
                "last_check": LAST_CHECK, "last_change": LAST_CHANGE,
                "status_information": "Synthetic alert — threshold exceeded ✓",
            }))
            index += 1
    return b"[" + b",".join(records) + b"]"


def get_json(base: str, path: str) -> tuple[dict, float]:
    started = time.perf_counter()
    with urlopen(base + path, timeout=60) as response:
        result = json.loads(response.read())
    return result, time.perf_counter() - started


def main() -> None:
    base = os.environ.get("NAFTAMON_SCALE_URL", "http://web:8000").rstrip("/")
    started = time.perf_counter()
    alerts = make_alerts()
    statuses = make_statuses()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    prefix = b'{"alerts":' + alerts + b',"complete":true,"ok_statuses":'
    suffix = (b',"servers":[{"server":"' + SERVER.encode() + b'","status":"ok"}],'
              b'"source":"NaftaMon","timestamp":"' + timestamp.encode() + b'"}')
    raw = prefix + statuses + suffix
    serialization_seconds = time.perf_counter() - started
    compressed_started = time.perf_counter()
    compressed = gzip.compress(raw, compresslevel=6, mtime=0)
    compression_seconds = time.perf_counter() - compressed_started

    request = Request(
        base + "/api/v1/ingest", data=compressed, method="POST",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip",
                 "User-Agent": "NaftaMon Scale Fixture"},
    )
    send_started = time.perf_counter()
    with urlopen(request, timeout=180) as response:
        ingest_result = json.loads(response.read())
        status_code = response.status
        server_timing = response.headers.get("Server-Timing", "")
        wire_bytes = int(response.headers.get("X-NaftaMon-Request-Bytes", "0"))
        json_bytes = int(response.headers.get("X-NaftaMon-JSON-Bytes", "0"))
    send_seconds = time.perf_counter() - send_started
    assert status_code == 202 and ingest_result["accepted"] is True, ingest_result

    # Simulate a later regular Gateway interval carrying unchanged monitoring
    # data. This measures the common refresh path independently of first insert.
    repeated_timestamp = (datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    repeated_json = raw.replace(timestamp.encode(), repeated_timestamp.encode(), 1)
    repeated_compress_started = time.perf_counter()
    repeated_gzip = gzip.compress(repeated_json, compresslevel=6, mtime=0)
    repeated_compression_seconds = time.perf_counter() - repeated_compress_started
    repeated_request = Request(
        base + "/api/v1/ingest", data=repeated_gzip, method="POST",
        headers={"Content-Type": "application/json", "Content-Encoding": "gzip",
                 "User-Agent": "NaftaMon Scale Fixture"},
    )
    repeated_send_started = time.perf_counter()
    with urlopen(repeated_request, timeout=180) as response:
        repeated_result = json.loads(response.read())
        repeated_server_timing = response.headers.get("Server-Timing", "")
    repeated_send_seconds = time.perf_counter() - repeated_send_started
    assert repeated_result["accepted"] is True
    assert repeated_result["content_hash"] == ingest_result["content_hash"]

    stats, stats_seconds = get_json(base, "/api/v1/stats")
    first, page_seconds = get_json(base, "/api/v1/statuses?limit=50&offset=0")
    filtered, search_seconds = get_json(base, "/api/v1/statuses?query=ALL%20CHECKS&limit=25&offset=0")
    health, _ = get_json(base, "/healthz")
    assert health["status"] == "ok"
    assert first["total"] == OK_COUNT + sum(ALERT_COUNTS.values()) and len(first["items"]) == 50
    assert filtered["total"] == OK_COUNT and len(filtered["items"]) == 25
    assert stats["alerts"]["total"] == sum(ALERT_COUNTS.values())
    assert stats["alerts"]["by_state"] == ALERT_COUNTS
    assert stats["monitoring"]["total"] == OK_COUNT + sum(ALERT_COUNTS.values())
    assert stats["monitoring"]["by_state"]["UP"] == OK_COUNT
    assert sum(stats["alerts"]["by_state"].values()) == stats["alerts"]["total"]
    # The fixture gives every status a unique host name; successful validation
    # also proves the model accepted the full identity set and cross-array check.

    print(json.dumps({
        "fixture": {"ok_statuses": OK_COUNT, "alerts": sum(ALERT_COUNTS.values()),
                    "unique_ok_identities": OK_COUNT, "alert_counts": ALERT_COUNTS},
        "bytes": {"alerts": len(alerts), "ok_statuses": len(statuses),
                  "metadata": len(raw) - len(alerts) - len(statuses), "json": len(raw),
                  "gzip": len(compressed), "wire_bytes_received": wire_bytes,
                  "json_bytes_received": json_bytes,
                  "json_mib": round(len(raw) / 1024 / 1024, 3),
                  "gzip_mib": round(len(compressed) / 1024 / 1024, 3),
                  "ratio": round(len(compressed) / len(raw), 5)},
        "seconds": {"serialize_and_assemble": round(serialization_seconds, 3),
                    "gzip_level_6": round(compression_seconds, 3),
                    "http_send_and_ingest": round(send_seconds, 3),
                    "repeat_compress": round(repeated_compression_seconds, 3),
                    "repeat_http_send_and_ingest": round(repeated_send_seconds, 3),
                    "repeat_server_timing": repeated_server_timing,
                    "api_stats": round(stats_seconds, 3),
                    "api_first_50": round(page_seconds, 3),
                    "api_status_information_search": round(search_seconds, 3),
                    "server_timing": server_timing},
        "server": {"accepted": ingest_result, "stats": stats,
                   "page_total": first["total"], "page_rows": len(first["items"]),
                   "search_total": filtered["total"]},
        "fixture_sha256": hashlib.sha256(raw).hexdigest(),
        "client_peak_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
