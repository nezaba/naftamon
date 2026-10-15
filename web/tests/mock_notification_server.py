"""Internal-only HTTP sink for Docker notification integration checks."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Lock
from urllib.parse import urlsplit


_lock = Lock()
_received: list[dict] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Test headers and bodies may contain fake tokens; keep container logs quiet.
        pass

    def _json(self, status: int, payload: object):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path != "/received":
            self._json(404, {"error": "not found"})
            return
        with _lock:
            items = list(_received)
        self._json(200, {"items": items, "total": len(items)})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > 65536:
            self._json(413, {"error": "body too large"})
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json(400, {"error": "invalid JSON"})
            return
        with _lock:
            _received.append({
                "path": urlsplit(self.path).path,
                "body": body,
                "header_names": sorted(self.headers.keys()),
            })
        if self.path.startswith("/fail/500"):
            self._json(500, {"error": "mock failure"})
        elif self.path.startswith("/fail/429"):
            self.send_response(429)
            self.send_header("Retry-After", "1")
            self.send_header("Content-Length", "0")
            self.end_headers()
        else:
            self._json(202, {"accepted": True})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8765), Handler).serve_forever()
