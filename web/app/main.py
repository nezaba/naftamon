from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .db import connect, initialize
from .ingest import accept_snapshot, iso_utc
from .models import Snapshot, TestMessage
from .settings import Settings


INGEST_PATH = "/api/v1/ingest"


class RequestSizeLimitMiddleware:
    """Buffer only the bounded ingest body and return 413 before parsing it."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != INGEST_PATH or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if int(length) > self.max_bytes:
                    await self._too_large(scope, receive, send)
                    return
                if int(length) < 0:
                    await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
                    return
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
                return

        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await self._too_large(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        delivered = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay_receive, send)

    async def _too_large(self, scope, receive, send):
        response = JSONResponse({"detail": "Request body exceeds the configured size limit"}, status_code=413)
        await response(scope, receive, send)


def _bool(value: int) -> bool:
    return bool(value)


def _stale_clause() -> str:
    return "((julianday('now') - julianday(s.last_received_at)) * 86400 > ? OR ms.status != 'ok' OR ms.status_received_at < s.last_received_at OR ms.removed = 1)"


def _alert_dict(row, stale: bool) -> dict[str, Any]:
    return {
        "id": row["id"],
        "source_id": row["source_id"],
        "source": row["source_name"],
        "peer_ip": row["peer_ip"],
        "server": row["server"],
        "backend": row["backend"],
        "host": row["host"],
        "service": row["service"],
        "type": row["type"],
        "state": row["state"],
        "hard": _bool(row["hard"]),
        "acknowledged": _bool(row["acknowledged"]),
        "downtime": _bool(row["downtime"]),
        "last_check": row["last_check"],
        "last_change": row["last_change"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "stale": stale,
    }


def create_app(
    settings: Settings | None = None,
    database_path: Path | None = None,
    offline_after_seconds: int | None = None,
    history_retention_days: int | None = None,
) -> FastAPI:
    base = settings or Settings.from_environment()
    app_settings = Settings(
        database_path=database_path or base.database_path,
        max_request_bytes=base.max_request_bytes,
        offline_after_seconds=offline_after_seconds or base.offline_after_seconds,
        history_retention_days=history_retention_days if history_retention_days is not None else base.history_retention_days,
    )
    initialize(app_settings.database_path)
    app = FastAPI(title="NaftaMon Web API", version="1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = app_settings
    app.add_middleware(RequestSizeLimitMiddleware, max_bytes=app_settings.max_request_bytes)

    @app.get("/", response_class=PlainTextResponse)
    def root():
        return "NaftaMon Web backend is running. API: /api/v1; health: /healthz."

    @app.post(INGEST_PATH, status_code=202)
    async def ingest(request: Request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
        try:
            raw = await request.body()
            payload = await run_in_threadpool(json.loads, raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"detail": "Request body must contain valid UTF-8 JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"detail": "JSON root must be an object"}, status_code=422)

        try:
            if payload.get("type") == "test":
                message = await run_in_threadpool(TestMessage.model_validate, payload)
                # A test checks the HTTP path only; it does not create/refresh sources or snapshots.
                return JSONResponse(
                    {"accepted": True, "kind": "test", "source": message.source, "received_at": iso_utc()},
                    status_code=202,
                )
            snapshot = await run_in_threadpool(Snapshot.model_validate, payload)
        except ValidationError as exc:
            details = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
            return JSONResponse({"detail": details}, status_code=422)

        peer_ip = request.client.host if request.client else "unknown"
        try:
            result = await run_in_threadpool(
                accept_snapshot,
                app_settings.database_path,
                snapshot,
                peer_ip,
                app_settings.history_retention_days,
            )
        except sqlite3.Error:
            # Do not leak local database details to an unauthenticated LAN client.
            return JSONResponse({"detail": "Snapshot could not be stored"}, status_code=503)
        result["accepted"] = True
        return JSONResponse(result, status_code=202)

    @app.get("/api/v1/sources")
    def list_sources():
        now = datetime.now(timezone.utc)
        threshold = app_settings.offline_after_seconds
        with connect(app_settings.database_path) as db:
            sources = db.execute("SELECT * FROM sources ORDER BY source_name, peer_ip").fetchall()
            result = []
            for source in sources:
                age = max(0, (now - datetime.fromisoformat(source["last_received_at"].replace("Z", "+00:00"))).total_seconds())
                servers = db.execute(
                    "SELECT name, status, status_received_at, last_ok_at, removed FROM monitored_servers WHERE source_id = ? ORDER BY name",
                    (source["id"],),
                ).fetchall()
                result.append(
                    {
                        "id": source["id"], "source": source["source_name"], "peer_ip": source["peer_ip"],
                        "first_received_at": source["first_received_at"], "last_received_at": source["last_received_at"],
                        "payload_timestamp": source["payload_timestamp"], "complete": _bool(source["complete"]),
                        "age_seconds": int(age), "offline": age > threshold,
                        "servers": [
                            {"name": server["name"], "status": server["status"],
                             "status_received_at": server["status_received_at"], "last_ok_at": server["last_ok_at"],
                             "removed": _bool(server["removed"]),
                             "stale": server["removed"] or server["status"] != "ok" or server["status_received_at"] < source["last_received_at"]}
                            for server in servers
                        ],
                    }
                )
        return {"offline_after_seconds": threshold, "items": result}

    @app.get("/api/v1/alerts")
    def list_alerts(
        source_id: int | None = None,
        server: str | None = Query(default=None, max_length=255),
        state: str | None = Query(default=None, max_length=32),
        alert_type: str | None = Query(default=None, alias="type", pattern="^(host|service)$"),
        stale: bool | None = None,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        conditions = []
        params: list[Any] = []
        if source_id is not None:
            conditions.append("a.source_id = ?")
            params.append(source_id)
        if server is not None:
            conditions.append("a.server = ?")
            params.append(server)
        if state is not None:
            conditions.append("a.state = ?")
            params.append(state.upper())
        if alert_type is not None:
            conditions.append("a.type = ?")
            params.append(alert_type)
        if stale is not None:
            conditions.append(f"{_stale_clause()} = ?")
            params.extend([app_settings.offline_after_seconds, int(stale)])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        base_query = """ FROM active_alerts a JOIN sources s ON s.id = a.source_id
                         JOIN monitored_servers ms ON ms.source_id = a.source_id AND ms.name = a.server"""
        with connect(app_settings.database_path) as db:
            total = db.execute("SELECT COUNT(*)" + base_query + where, params).fetchone()[0]
            rows = db.execute(
                """SELECT a.*, s.source_name, s.peer_ip,
                   """ + _stale_clause() + " AS stale_flag" + base_query + where +
                " ORDER BY a.state, a.server, a.host, a.service LIMIT ? OFFSET ?",
                [app_settings.offline_after_seconds, *params, limit, offset],
            ).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "items": [_alert_dict(row, _bool(row["stale_flag"])) for row in rows]}

    @app.get("/api/v1/stats")
    def stats():
        with connect(app_settings.database_path) as db:
            source_rows = db.execute("SELECT last_received_at FROM sources").fetchall()
            now = datetime.now(timezone.utc)
            online_sources = 0
            for row in source_rows:
                age = (now - datetime.fromisoformat(row["last_received_at"].replace("Z", "+00:00"))).total_seconds()
                if age <= app_settings.offline_after_seconds:
                    online_sources += 1
            counts = db.execute("SELECT state, COUNT(*) AS count FROM active_alerts GROUP BY state").fetchall()
            all_alerts = db.execute(
                """SELECT COUNT(*) AS total,
                   SUM(CASE WHEN """ + _stale_clause() + " THEN 1 ELSE 0 END) AS stale_count" +
                " FROM active_alerts a JOIN sources s ON s.id=a.source_id JOIN monitored_servers ms ON ms.source_id=a.source_id AND ms.name=a.server",
                [app_settings.offline_after_seconds],
            ).fetchone()
        return {
            "sources": {"total": len(source_rows), "online": online_sources, "offline": len(source_rows) - online_sources},
            "alerts": {"total": all_alerts["total"], "stale": all_alerts["stale_count"] or 0,
                       "by_state": {row["state"]: row["count"] for row in counts}},
        }

    @app.get("/api/v1/history")
    def history(
        source_id: int | None = None,
        server: str | None = Query(default=None, max_length=255),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        conditions = []
        params: list[Any] = []
        if source_id is not None:
            conditions.append("h.source_id = ?")
            params.append(source_id)
        if server is not None:
            conditions.append("h.server = ?")
            params.append(server)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with connect(app_settings.database_path) as db:
            total = db.execute("SELECT COUNT(*) FROM alert_history h" + where, params).fetchone()[0]
            rows = db.execute(
                """SELECT h.*, s.source_name, s.peer_ip FROM alert_history h
                   JOIN sources s ON s.id = h.source_id""" + where +
                " ORDER BY h.observed_at DESC, h.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {"total": total, "limit": limit, "offset": offset,
                "items": [dict(row) for row in rows]}

    @app.get("/healthz")
    def healthz():
        try:
            with connect(app_settings.database_path) as db:
                db.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return JSONResponse({"status": "unhealthy", "database": "unavailable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    return app


app = create_app()
