#!/usr/bin/env python3
"""Local lab sink: print received JSON to container logs, one record per line."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

received = []
received_lock = threading.Lock()
stats = {"total": 0, "active": 0, "max_active": 0, "last_body_bytes": 0}
MAX_BODY_BYTES = 8 * 1024 * 1024


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
        if route.path != "/webhook":
            return self.reply(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > MAX_BODY_BYTES:
                return self.reply(413, {"error": "JSON body must be 1 byte to 8 MiB"})
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
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
        try:
            if delay:
                time.sleep(delay)
            if status != 202:
                return self.reply(status, {"received": False})
            with received_lock:
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
