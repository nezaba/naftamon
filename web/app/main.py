from __future__ import annotations

import json
import sqlite3
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .db import SCHEMA_VERSION, _fold_search, connect, initialize
from .ingest import accept_snapshot, iso_utc
from .models import Snapshot, TestMessage
from .notifications.api import router as notifications_router
from .settings import Settings
from .streaming import (
    STREAMING_SNAPSHOT_THRESHOLD_BYTES,
    SnapshotStreamValidationError,
    parse_streamed_snapshot,
)


INGEST_PATH = "/api/v1/ingest"


class RequestSizeLimitMiddleware:
    """Buffer only the bounded ingest body and return 413 before parsing it."""

    def __init__(self, app, max_bytes: int, max_compressed_bytes: int, max_decompressed_bytes: int):
        self.app = app
        self.max_bytes = max_bytes
        self.max_compressed_bytes = max_compressed_bytes
        self.max_decompressed_bytes = max_decompressed_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") != INGEST_PATH or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        encoding = headers.get(b"content-encoding", b"identity").decode("ascii", "ignore").strip().lower()
        if encoding not in ("", "identity", "gzip"):
            await JSONResponse({"detail": "Unsupported Content-Encoding"}, status_code=415)(scope, receive, send)
            return
        compressed = encoding == "gzip"
        wire_limit = self.max_compressed_bytes if compressed else self.max_bytes
        length = headers.get(b"content-length")
        if length is not None:
            try:
                if int(length) > wire_limit:
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
        decoded_size = 0
        decode_started = time.perf_counter()
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS) if compressed else None
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > wire_limit:
                await self._too_large(scope, receive, send)
                return
            if decompressor is None:
                chunks.append(chunk)
                decoded_size += len(chunk)
            else:
                try:
                    decoded = decompressor.decompress(chunk, self.max_decompressed_bytes - decoded_size + 1)
                except zlib.error:
                    await JSONResponse({"detail": "Invalid gzip request body"}, status_code=400)(scope, receive, send)
                    return
                decoded_size += len(decoded)
                if decoded_size > self.max_decompressed_bytes or decompressor.unconsumed_tail:
                    await self._too_large(scope, receive, send)
                    return
                if decoded:
                    chunks.append(decoded)
            if not message.get("more_body", False):
                break

        if decompressor is not None:
            try:
                decoded = decompressor.flush(self.max_decompressed_bytes - decoded_size + 1)
            except zlib.error:
                await JSONResponse({"detail": "Invalid gzip request body"}, status_code=400)(scope, receive, send)
                return
            decoded_size += len(decoded)
            if decoded_size > self.max_decompressed_bytes:
                await self._too_large(scope, receive, send)
                return
            if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
                await JSONResponse({"detail": "Invalid gzip request body"}, status_code=400)(scope, receive, send)
                return
            if decoded:
                chunks.append(decoded)

        body = b"".join(chunks)
        chunks.clear()
        request_state = scope.setdefault("state", {})
        request_state["naftamon_decode_ms"] = (time.perf_counter() - decode_started) * 1000
        request_state["naftamon_wire_bytes"] = size
        request_state["naftamon_json_bytes"] = decoded_size
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
    return "((julianday('now') - julianday(s.last_received_at)) * 86400 > ? OR ms.status != 'ok' OR ms.status_revision < s.snapshot_revision OR ms.removed = 1)"


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
        "status_information": row["status_information"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "stale": stale,
    }


def create_app(
    settings: Settings | None = None,
    database_path: Path | None = None,
    offline_after_seconds: int | None = None,
    source_inactive_after_seconds: int | None = None,
    history_retention_days: int | None = None,
) -> FastAPI:
    base = settings or Settings.from_environment()
    app_settings = Settings(
        database_path=database_path or base.database_path,
        max_request_bytes=base.max_request_bytes,
        offline_after_seconds=offline_after_seconds if offline_after_seconds is not None else base.offline_after_seconds,
        source_inactive_after_seconds=(
            source_inactive_after_seconds if source_inactive_after_seconds is not None
            else base.source_inactive_after_seconds
        ),
        history_retention_days=history_retention_days if history_retention_days is not None else base.history_retention_days,
        max_compressed_request_bytes=base.max_compressed_request_bytes,
        max_decompressed_request_bytes=base.max_decompressed_request_bytes,
    )
    initialize(app_settings.database_path)
    app = FastAPI(title="NaftaMon Web", version="1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = app_settings
    app.include_router(notifications_router)
    app.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=app_settings.max_request_bytes,
        max_compressed_bytes=app_settings.max_compressed_request_bytes,
        max_decompressed_bytes=app_settings.max_decompressed_request_bytes,
    )
    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.middleware("http")
    async def revalidate_dashboard_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/")
    def root():
        return FileResponse(static_dir / "index.html", media_type="text/html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")

    @app.post(INGEST_PATH, status_code=202)
    async def ingest(request: Request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse({"detail": "Content-Type must be application/json"}, status_code=415)
        validation_started = time.perf_counter()
        streamed_ok_statuses = None
        streamed_ok_statuses_present = False
        try:
            raw = await request.body()
            # The common (large snapshot) path lets pydantic-core parse directly
            # from bytes, avoiding a second full Python dict/list tree.
            payload = await run_in_threadpool(json.loads, raw) if len(raw) <= 4096 else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"detail": "Request body must contain valid UTF-8 JSON"}, status_code=400)
        if payload is not None and not isinstance(payload, dict):
            return JSONResponse({"detail": "JSON root must be an object"}, status_code=422)
        try:
            if payload is not None and payload.get("type") == "test":
                message = await run_in_threadpool(TestMessage.model_validate, payload)
                # A test checks the HTTP path only; it does not create/refresh sources or snapshots.
                return JSONResponse(
                    {"accepted": True, "kind": "test", "source": message.source, "received_at": iso_utc()},
                    status_code=202,
                )
            if len(raw) > STREAMING_SNAPSHOT_THRESHOLD_BYTES:
                parsed = await run_in_threadpool(parse_streamed_snapshot, raw)
                snapshot = parsed.snapshot
                streamed_ok_statuses = parsed.ok_statuses_file
                streamed_ok_statuses_present = parsed.ok_statuses_present
            else:
                snapshot = await run_in_threadpool(Snapshot.model_validate, payload) if payload is not None else await run_in_threadpool(Snapshot.model_validate_json, raw)
        except ValidationError as exc:
            details = [{"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]} for error in exc.errors()]
            if any(error["type"] == "json_invalid" for error in exc.errors()):
                return JSONResponse({"detail": "Request body must contain valid UTF-8 JSON"}, status_code=400)
            return JSONResponse({"detail": details}, status_code=422)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return JSONResponse({"detail": "Request body must contain valid UTF-8 JSON"}, status_code=400)
        except SnapshotStreamValidationError as exc:
            return JSONResponse({"detail": exc.detail}, status_code=422)
        validation_ms = (time.perf_counter() - validation_started) * 1000

        peer_ip = request.client.host if request.client else "unknown"
        persistence_started = time.perf_counter()
        try:
            result = await run_in_threadpool(
                accept_snapshot,
                app_settings.database_path,
                snapshot,
                peer_ip,
                app_settings.history_retention_days,
                raw,
                streamed_ok_statuses,
                streamed_ok_statuses_present,
            )
        except sqlite3.Error:
            # Do not leak local database details to an unauthenticated LAN client.
            return JSONResponse({"detail": "Snapshot could not be stored"}, status_code=503)
        finally:
            if streamed_ok_statuses is not None:
                streamed_ok_statuses.close()
        result["accepted"] = True
        response = JSONResponse(result, status_code=202)
        state = request.scope.get("state", {})
        response.headers["Server-Timing"] = (
            f"decode;dur={state.get('naftamon_decode_ms', 0):.1f}, "
            f"validate;dur={validation_ms:.1f}, "
            f"persist;dur={(time.perf_counter() - persistence_started) * 1000:.1f}"
        )
        response.headers["X-NaftaMon-Request-Bytes"] = str(state.get("naftamon_wire_bytes", len(raw)))
        response.headers["X-NaftaMon-JSON-Bytes"] = str(state.get("naftamon_json_bytes", len(raw)))
        return response

    @app.get("/api/v1/sources")
    def list_sources():
        now = datetime.now(timezone.utc)
        threshold = app_settings.offline_after_seconds
        inactive_threshold = app_settings.source_inactive_after_seconds
        with connect(app_settings.database_path) as db:
            sources = db.execute(
                "SELECT * FROM sources WHERE deleted_at IS NULL ORDER BY source_name, peer_ip"
            ).fetchall()
            alert_counts = {
                row["source_id"]: row["count"]
                for row in db.execute("SELECT source_id, COUNT(*) AS count FROM active_alerts GROUP BY source_id")
            }
            backends = [row["backend"] for row in db.execute(
                """SELECT backend FROM active_alerts WHERE source_id IN
                       (SELECT id FROM sources WHERE deleted_at IS NULL)
                   UNION SELECT backend FROM monitored_statuses WHERE source_id IN
                       (SELECT id FROM sources WHERE deleted_at IS NULL)
                   ORDER BY backend"""
            )]
            result = []
            for source in sources:
                age = max(0, (now - datetime.fromisoformat(source["last_received_at"].replace("Z", "+00:00"))).total_seconds())
                servers = db.execute(
                    "SELECT name, status, status_received_at, status_revision, last_ok_at, removed FROM monitored_servers WHERE source_id = ? ORDER BY name",
                    (source["id"],),
                ).fetchall()
                result.append(
                    {
                        "id": source["id"], "source": source["source_name"], "peer_ip": source["peer_ip"],
                        "first_received_at": source["first_received_at"], "last_received_at": source["last_received_at"],
                        "payload_timestamp": source["payload_timestamp"], "complete": _bool(source["complete"]),
                        "age_seconds": int(age), "offline": age > threshold,
                        "inactive": age > inactive_threshold,
                        "alert_count": alert_counts.get(source["id"], 0),
                        "servers": [
                            {"name": server["name"], "status": server["status"],
                             "status_received_at": server["status_received_at"], "last_ok_at": server["last_ok_at"],
                             "removed": _bool(server["removed"]),
                             "stale": server["removed"] or server["status"] != "ok" or server["status_revision"] < source["snapshot_revision"]}
                            for server in servers
                        ],
                    }
                )
        return {
            "offline_after_seconds": threshold,
            "inactive_after_seconds": inactive_threshold,
            "backends": backends,
            "items": result,
        }

    @app.delete("/api/v1/sources/{source_id}")
    def delete_inactive_source(source_id: int):
        if source_id < 1:
            raise HTTPException(status_code=404, detail="Source not found")
        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        try:
            with connect(app_settings.database_path) as db:
                db.execute("BEGIN IMMEDIATE")
                source = db.execute(
                    "SELECT id, last_received_at FROM sources WHERE id = ? AND deleted_at IS NULL",
                    (source_id,),
                ).fetchone()
                if source is None:
                    raise HTTPException(status_code=404, detail="Source not found")
                last_received = datetime.fromisoformat(source["last_received_at"].replace("Z", "+00:00"))
                if (now - last_received).total_seconds() <= app_settings.source_inactive_after_seconds:
                    raise HTTPException(status_code=409, detail="Only inactive sources can be removed")

                # Keep event/delivery/attempt rows as notification history. Stop work
                # that has not completed so removal cannot trigger later sends.
                db.execute(
                    """UPDATE notification_deliveries
                       SET status = 'cancelled', lease_until = NULL, updated_at = ?,
                           last_error = 'Source manually removed'
                       WHERE status IN ('pending', 'in_flight')
                         AND event_id IN (SELECT id FROM notification_events WHERE source_id = ?)""",
                    (now_text, source_id),
                )
                db.execute("DELETE FROM notification_suppression_state WHERE source_id = ?", (source_id,))
                for table in (
                    "latest_snapshots", "active_alerts", "monitored_statuses",
                    "monitored_servers", "alert_history",
                ):
                    db.execute(f"DELETE FROM {table} WHERE source_id = ?", (source_id,))
                # A hidden tombstone preserves notification_events.source_id and
                # the unique emitter key, without keeping its old receipt metadata.
                db.execute(
                    """UPDATE sources SET deleted_at = ?, first_received_at = ?, last_received_at = ?,
                       payload_timestamp = ?, complete = 0, snapshot_revision = 0 WHERE id = ?""",
                    (now_text, now_text, now_text, now_text, source_id),
                )
                db.execute("UPDATE app_state SET value = value + 1 WHERE key = 'monitoring_revision'")
        except sqlite3.Error:
            return JSONResponse({"detail": "Source could not be removed"}, status_code=503)
        return {"deleted": True, "source_id": source_id, "reconnectable": True}

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

    @app.get("/api/v1/statuses")
    def list_statuses(
        source_id: int | None = None,
        server: str | None = Query(default=None, max_length=255),
        backend: str | None = Query(default=None, max_length=255),
        host: str | None = Query(default=None, max_length=255),
        service: str | None = Query(default=None, max_length=255),
        state: str | None = Query(default=None, max_length=32),
        status_type: str | None = Query(default=None, alias="type", pattern="^(host|service)$"),
        host_query: str | None = Query(default=None, max_length=255),
        service_query: str | None = Query(default=None, max_length=255),
        query: str | None = Query(default=None, max_length=255),
        acknowledged: bool | None = None,
        downtime: bool | None = None,
        revision: int | None = Query(default=None, ge=0),
        sort: str = Query(default="state", pattern="^(source|peer_ip|server|backend|host|service|state|status_information|acknowledged|downtime|last_seen_at)$"),
        direction: str = Query(default="asc", pattern="^(asc|desc)$"),
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ):
        """Return a filtered, server-paginated view of current monitoring states."""
        union_query = f"""SELECT * FROM (
            SELECT 'alert:' || a.id AS id, a.source_id, s.source_name AS source, s.peer_ip,
                   a.server, a.backend, a.host, a.service, a.type, a.state,
                   a.status_information, a.hard, a.acknowledged, a.downtime,
                   a.search_text, a.host_search, a.service_search,
                   a.last_check, a.last_change, a.first_seen_at, a.last_seen_at,
                   {_stale_clause()} AS stale_flag
              FROM active_alerts a JOIN sources s ON s.id = a.source_id
              JOIN monitored_servers ms ON ms.source_id = a.source_id AND ms.name = a.server
            UNION ALL
            SELECT 'status:' || m.id AS id, m.source_id, s.source_name AS source, s.peer_ip,
                   m.server, m.backend, m.host, m.service, m.type, m.state,
                   m.status_information, m.hard, m.acknowledged, m.downtime,
                   m.search_text, m.host_search, m.service_search,
                   m.last_check, m.last_change, m.first_seen_at,
                   CASE WHEN ms.status = 'ok' THEN ms.status_received_at ELSE COALESCE(ms.last_ok_at, m.last_seen_at) END AS last_seen_at,
                   CASE WHEN ((julianday('now') - julianday(s.last_received_at)) * 86400 > ?
                     OR ms.status != 'ok' OR ms.status_revision < s.snapshot_revision
                     OR ms.status_data_revision < s.snapshot_revision OR ms.removed = 1) THEN 1 ELSE 0 END AS stale_flag
              FROM monitored_statuses m JOIN sources s ON s.id = m.source_id
              JOIN monitored_servers ms ON ms.source_id = m.source_id AND ms.name = m.server
        )"""
        conditions = []
        params: list[Any] = [app_settings.offline_after_seconds, app_settings.offline_after_seconds]
        for column, value in (("source_id", source_id), ("server", server), ("backend", backend),
                              ("host", host), ("service", service), ("type", status_type)):
            if value is not None:
                conditions.append(f"{column} = ?")
                params.append(value)
        if state is not None:
            conditions.append("state = ?")
            params.append(state.upper())
        for column, value in (("host", host_query), ("service", service_query)):
            if value:
                search_column = "host_search" if column == "host" else "service_search"
                conditions.append(f"instr({search_column}, ?) > 0")
                params.append(_fold_search(value))
        query_condition_index = None
        query_param_index = None
        if query:
            # Monitoring rows carry normalized search text updated during ingest.
            # Source name/IP are small-dimension fields, so fold those once per
            # source instead of invoking a Python SQLite UDF for every status row.
            source_search = _fold_search(query)
            query_condition_index = len(conditions)
            query_param_index = len(params)
            conditions.append("instr(search_text, ?) > 0")
            params.append(source_search)
        if acknowledged is not None:
            conditions.append("acknowledged = ?")
            params.append(int(acknowledged))
        if downtime is not None:
            conditions.append("downtime = ?")
            params.append(int(downtime))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        sort_columns = {
            "source": "source", "peer_ip": "peer_ip", "server": "server", "backend": "backend",
            "host": "host", "service": "service", "state": "state_rank",
            "status_information": "status_information", "acknowledged": "acknowledged",
            "downtime": "downtime", "last_seen_at": "last_seen_at",
        }
        sort_column = sort_columns[sort]
        sort_direction = direction.upper()
        with connect(app_settings.database_path) as db:
            if query:
                source_matches = [
                    row[0] for row in db.execute(
                        "SELECT id FROM sources WHERE instr(nafta_fold(source_name || ' ' || peer_ip), ?) > 0",
                        (source_search,),
                    )
                ]
                if source_matches:
                    conditions[query_condition_index] = "(instr(search_text, ?) > 0 OR source_id IN (" + ",".join("?" for _ in source_matches) + "))"
                    params[query_param_index + 1:query_param_index + 1] = source_matches
            current_revision = db.execute(
                "SELECT value FROM app_state WHERE key = 'monitoring_revision'"
            ).fetchone()[0]
            if revision is not None and revision != current_revision:
                return JSONResponse({"detail": "Monitoring changed while paging", "revision": current_revision}, status_code=409)
            total = db.execute("SELECT COUNT(*) FROM (" + union_query + ") AS current_statuses" + where, params).fetchone()[0]
            rows = db.execute(
                "SELECT current_statuses.*, CASE state WHEN 'CRITICAL' THEN 0 WHEN 'DOWN' THEN 1 "
                "WHEN 'UNREACHABLE' THEN 2 WHEN 'WARNING' THEN 3 WHEN 'UNKNOWN' THEN 4 ELSE 5 END AS state_rank "
                "FROM (" + union_query + ") AS current_statuses" + where +
                f" ORDER BY {sort_column} {sort_direction}, server, host, service LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["hard"] = _bool(item["hard"])
            item["acknowledged"] = _bool(item["acknowledged"])
            item["downtime"] = _bool(item["downtime"])
            item["stale"] = _bool(item["stale_flag"])
            del item["stale_flag"]
            item.pop("state_rank", None)
            items.append(item)
        return {"total": total, "limit": limit, "offset": offset, "revision": current_revision, "items": items}

    @app.get("/api/v1/stats")
    def stats():
        with connect(app_settings.database_path) as db:
            source_rows = db.execute(
                "SELECT last_received_at FROM sources WHERE deleted_at IS NULL"
            ).fetchall()
            now = datetime.now(timezone.utc)
            online_sources = 0
            for row in source_rows:
                age = (now - datetime.fromisoformat(row["last_received_at"].replace("Z", "+00:00"))).total_seconds()
                if age <= app_settings.offline_after_seconds:
                    online_sources += 1
            counts = db.execute("SELECT state, COUNT(*) AS count FROM active_alerts GROUP BY state").fetchall()
            alert_types = db.execute(
                "SELECT state, type, COUNT(*) AS count FROM active_alerts GROUP BY state, type"
            ).fetchall()
            affected_hosts = db.execute(
                """SELECT COUNT(*) FROM (
                       SELECT source_id, server, host FROM active_alerts
                        WHERE type = 'host' AND state IN ('DOWN', 'UNREACHABLE')
                        GROUP BY source_id, server, host
                   )"""
            ).fetchone()[0]
            monitoring_counts = db.execute(
                """SELECT state, type, acknowledged, downtime, COUNT(*) AS count
                     FROM (
                       SELECT state, type, acknowledged, downtime FROM active_alerts
                       UNION ALL
                       SELECT state, type, acknowledged, downtime FROM monitored_statuses
                     ) GROUP BY state, type, acknowledged, downtime"""
            ).fetchall()
            all_alerts = db.execute(
                """SELECT COUNT(*) AS total,
                   SUM(CASE WHEN """ + _stale_clause() + " THEN 1 ELSE 0 END) AS stale_count" +
                " FROM active_alerts a JOIN sources s ON s.id=a.source_id JOIN monitored_servers ms ON ms.source_id=a.source_id AND ms.name=a.server",
                [app_settings.offline_after_seconds],
            ).fetchone()
        return {
            "sources": {"total": len(source_rows), "online": online_sources, "offline": len(source_rows) - online_sources},
            "alerts": {"total": all_alerts["total"], "stale": all_alerts["stale_count"] or 0,
                       "by_state": {row["state"]: row["count"] for row in counts},
                       "scope": "global_unfiltered_active_alerts",
                       "by_state_and_type": {
                           f"{row['state']}:{row['type']}": row["count"] for row in alert_types
                       }, "affected_hosts": affected_hosts},
            "monitoring": {
                "scope": "global_unfiltered_current_monitoring",
                "total": sum(row["count"] for row in monitoring_counts),
                "by_state": {
                    state: sum(row["count"] for row in monitoring_counts if row["state"] == state)
                    for state in sorted({row["state"] for row in monitoring_counts})
                },
                "hosts": sum(row["count"] for row in monitoring_counts if row["type"] == "host"),
                "services": sum(row["count"] for row in monitoring_counts if row["type"] == "service"),
                "acknowledged": sum(row["count"] for row in monitoring_counts if row["acknowledged"]),
                "downtime": sum(row["count"] for row in monitoring_counts if row["downtime"]),
            },
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
                version = db.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
                tables = {
                    row["name"] for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                required_tables = {
                    "schema_version", "sources", "latest_snapshots", "monitored_servers",
                    "active_alerts", "monitored_statuses", "alert_history",
                }
                if version != SCHEMA_VERSION or not required_tables.issubset(tables):
                    return JSONResponse({"status": "unhealthy", "database": "unavailable"}, status_code=503)
        except sqlite3.Error:
            return JSONResponse({"status": "unhealthy", "database": "unavailable"}, status_code=503)
        return {"status": "ok", "database": "ok"}

    return app


app = create_app()
