#!/usr/bin/env python3
"""Local lab sink: print received JSON to container logs, one record per line."""

import json
import gzip
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

received = []
received_lock = threading.Lock()
stats = {"total": 0, "active": 0, "max_active": 0, "last_body_bytes": 0,
         "last_decoded_bytes": 0, "last_content_encoding": "identity"}
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_DECODED_BYTES = 128 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.reply(200, {"status": "ok"})
        elif self.path == "/received":
            with received_lock:
                self.reply(200, list(received))
        elif self.path == "/stats":
            with received_lock:
                self.reply(200, dict(stats))
        else:
            self.reply(404, {"error": "not found"})

    def do_POST(self):
        route = urlparse(self.path)
        if route.path not in ("/webhook", "/legacy", "/old"):
            return self.reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY_BYTES:
                return self.reply(413, {"error": "Request body must be 1 byte to 8 MiB"})
            body = self.rfile.read(length)
            encoding = self.headers.get("Content-Encoding", "identity").lower()
            if route.path == "/old" and encoding == "gzip":
                with received_lock:
                    stats["total"] += 1
                    stats["gzip_rejected"] = stats.get("gzip_rejected", 0) + 1
                    stats["last_body_bytes"] = length
                    stats["last_content_encoding"] = encoding
                return self.reply(400, {"error": "legacy receiver does not support gzip"})
            if encoding == "gzip":
                body = gzip.decompress(body)
            elif encoding not in ("", "identity"):
                return self.reply(415, {"error": "unsupported content encoding"})
            if len(body) > MAX_DECODED_BYTES:
                return self.reply(413, {"error": "decoded JSON body exceeds 128 MiB"})
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError, OSError):
            return self.reply(400, {"error": "invalid JSON"})
        options = parse_qs(route.query)
        try:
            delay = min(5.0, max(0.0, float(options.get("delay", ["0"])[0])))
            status = int(options.get("status", ["202"])[0])
        except ValueError:
            return self.reply(400, {"error": "invalid option"})
        with received_lock:
            stats["total"] += 1
            stats["active"] += 1
            stats["max_active"] = max(stats["max_active"], stats["active"])
            stats["last_body_bytes"] = length
            stats["last_decoded_bytes"] = len(body)
            stats["last_content_encoding"] = encoding or "identity"
        try:
            if delay:
                time.sleep(delay)
            rejected_fields = []
            for field in ("schema_version", "source_id", "snapshot_id", "summary"):
                if field in payload:
                    rejected_fields.append(field)
            if any("visible_in_desktop" in item for item in payload.get("alerts", [])):
                rejected_fields.append("visible_in_desktop")
            if "ok_statuses" in payload:
                rejected_fields.append("ok_statuses")
            if any("status_information" in item for item in payload.get("alerts", [])):
                rejected_fields.append("status_information")
            if route.path == "/legacy" and rejected_fields:
                with received_lock:
                    stats["extended_rejected"] = stats.get("extended_rejected", 0) + 1
                return self.reply(422, {"detail": [
                    {"type": "extra_forbidden", "loc": ["body", field],
                     "msg": "Extra inputs are not permitted"}
                    for field in rejected_fields
                ]})
            if status != 202:
                return self.reply(status, {"received": False})
            with received_lock:
                if options.get("summary") == ["1"]:
                    alerts = payload.get("alerts", [])
                    statuses = payload.get("ok_statuses", [])
                    alert_keys = {
                        (row.get("server"), row.get("backend"), row.get("type"), row.get("host"), row.get("service"))
                        for row in alerts
                    }
                    status_keys = {
                        (row.get("server"), row.get("backend"), row.get("type"), row.get("host"), row.get("service"))
                        for row in statuses
                    }
                    payload = {
                        "summary": True,
                        "schema_version": payload.get("schema_version", 1),
                        "source_id": payload.get("source_id"),
                        "snapshot_id": payload.get("snapshot_id"),
                        "alerts": len(alerts),
                        "alert_states": dict(Counter(row.get("state") for row in alerts)),
                        "alert_types": dict(Counter(row.get("type") for row in alerts)),
                        "raw_summary": payload.get("summary", {}).get("raw"),
                        "visible_summary": payload.get("summary", {}).get("visible"),
                        "alert_acknowledged": sum(bool(row.get("acknowledged")) for row in alerts),
                        "alert_downtime": sum(bool(row.get("downtime")) for row in alerts),
                        "ok_statuses": len(statuses),
                        "status_types": dict(Counter(row.get("type") for row in statuses)),
                        "unique_alerts": len(alert_keys),
                        "unique_ok_statuses": len(status_keys),
                        "identity_overlap": len(alert_keys & status_keys),
                        "first_status_information": statuses[0].get("status_information") if statuses else None,
                        "last_status_information": statuses[-1].get("status_information") if statuses else None,
                        "wire_contains_secret": b'"secret"' in body,
                    }
                received.append(payload)
                del received[:-10]
            if options.get("quiet") != ["1"]:
                print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)
            self.reply(202, {"received": True})
        finally:
            with received_lock:
                stats["active"] -= 1

    def reply(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # expected when a gateway timeout aborts the request


ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
