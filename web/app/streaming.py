"""Bounded-memory parsing for snapshots with very large ok_statuses arrays."""

from dataclasses import dataclass
import json
from tempfile import SpooledTemporaryFile
from typing import BinaryIO

from pydantic import ValidationError

from .models import OkStatus, Snapshot


STREAMING_SNAPSHOT_THRESHOLD_BYTES = 1024 * 1024
MAX_OK_STATUSES = 200_000
_ROOT_FIELDS = {
    "schema_version", "timestamp", "source", "source_id", "snapshot_id",
    "complete", "servers", "alerts", "ok_statuses", "summary",
}


class SnapshotStreamValidationError(ValueError):
    def __init__(self, detail: list[dict]):
        super().__init__("Snapshot validation failed")
        self.detail = detail


@dataclass
class StreamedSnapshot:
    snapshot: Snapshot
    ok_statuses_file: BinaryIO | None
    ok_statuses_present: bool


def _skip_whitespace(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _validation_detail(error: ValidationError, prefix: tuple = ()) -> list[dict]:
    return [
        {"loc": [*prefix, *item["loc"]], "msg": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]


def parse_streamed_snapshot(raw: bytes) -> StreamedSnapshot:
    """Parse one top-level object while spooling each UP record to disk.

    All non-status fields are bounded in this contract (at most 200,000 alerts,
    but production snapshots contain only the relatively small alert set). Each
    UP status is validated and released before the next is decoded.
    """
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    index = _skip_whitespace(text, 0)
    if index >= len(text) or text[index] != "{":
        raise SnapshotStreamValidationError([{
            "loc": [], "msg": "JSON root must be an object", "type": "value_error",
        }])
    index += 1
    root: dict = {}
    seen_fields: set[str] = set()
    status_file: BinaryIO | None = None
    status_keys: set[tuple[str, str, str, str, str]] = set()
    status_count = 0
    statuses_present = False
    try:
        index = _skip_whitespace(text, index)
        if index < len(text) and text[index] == "}":
            index += 1
        else:
            while True:
                index = _skip_whitespace(text, index)
                key, index = decoder.raw_decode(text, index)
                if not isinstance(key, str):
                    raise SnapshotStreamValidationError([{
                        "loc": [], "msg": "Snapshot object keys must be strings", "type": "value_error",
                    }])
                if key in seen_fields:
                    raise SnapshotStreamValidationError([{
                        "loc": [key], "msg": "Duplicate top-level JSON field", "type": "value_error",
                    }])
                seen_fields.add(key)
                if key not in _ROOT_FIELDS:
                    raise SnapshotStreamValidationError([{
                        "loc": [key], "msg": "Extra inputs are not permitted", "type": "extra_forbidden",
                    }])
                index = _skip_whitespace(text, index)
                if index >= len(text) or text[index] != ":":
                    raise json.JSONDecodeError("Expected ':' after object key", text, index)
                index = _skip_whitespace(text, index + 1)

                if key != "ok_statuses":
                    value, index = decoder.raw_decode(text, index)
                    root[key] = value
                else:
                    statuses_present = True
                    if index >= len(text) or text[index] != "[":
                        raise SnapshotStreamValidationError([{
                            "loc": ["ok_statuses"], "msg": "Input should be a valid array", "type": "list_type",
                        }])
                    status_file = SpooledTemporaryFile(max_size=4 * 1024 * 1024, mode="w+b")
                    index += 1
                    index = _skip_whitespace(text, index)
                    if index < len(text) and text[index] == "]":
                        index += 1
                    else:
                        while True:
                            if status_count >= MAX_OK_STATUSES:
                                raise SnapshotStreamValidationError([{
                                    "loc": ["ok_statuses"], "msg": f"List should contain at most {MAX_OK_STATUSES} items",
                                    "type": "too_long",
                                }])
                            value, index = decoder.raw_decode(text, index)
                            try:
                                status = OkStatus.model_validate(value)
                            except ValidationError as error:
                                raise SnapshotStreamValidationError(
                                    _validation_detail(error, ("ok_statuses", status_count))
                                ) from error
                            identity = (
                                status.server, status.backend, status.type, status.host, status.service,
                            )
                            if identity in status_keys:
                                raise SnapshotStreamValidationError([{
                                    "loc": ["ok_statuses", status_count],
                                    "msg": "ok_statuses must have unique composite identities",
                                    "type": "value_error",
                                }])
                            status_keys.add(identity)
                            encoded = json.dumps(
                                status.model_dump(mode="json", exclude_unset=True),
                                ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                            ).encode("utf-8")
                            status_file.write(encoded + b"\n")
                            status_count += 1
                            index = _skip_whitespace(text, index)
                            if index < len(text) and text[index] == ",":
                                index = _skip_whitespace(text, index + 1)
                                continue
                            if index < len(text) and text[index] == "]":
                                index += 1
                                break
                            raise json.JSONDecodeError("Expected ',' or ']' in array", text, index)

                index = _skip_whitespace(text, index)
                if index < len(text) and text[index] == ",":
                    index = _skip_whitespace(text, index + 1)
                    continue
                if index < len(text) and text[index] == "}":
                    index += 1
                    break
                raise json.JSONDecodeError("Expected ',' or '}' in object", text, index)

        if _skip_whitespace(text, index) != len(text):
            raise json.JSONDecodeError("Extra data after JSON object", text, index)
        try:
            snapshot = Snapshot.model_validate(root)
        except ValidationError:
            raise

        if statuses_present:
            server_status = {server.server: server.status for server in snapshot.servers}
            alert_keys = {
                (item.server, item.backend, item.type, item.host, item.service)
                for item in snapshot.alerts
            }
            for identity in status_keys:
                server, backend, item_type, host, service = identity
                if server_status.get(server) != "ok":
                    raise SnapshotStreamValidationError([{
                        "loc": ["ok_statuses"],
                        "msg": "ok_statuses may only be included for a server with status ok",
                        "type": "value_error",
                    }])
                if identity in alert_keys:
                    raise SnapshotStreamValidationError([{
                        "loc": ["ok_statuses"],
                        "msg": "alerts and ok_statuses must not share composite identities",
                        "type": "value_error",
                    }])
        if status_file is not None:
            status_file.seek(0)
        return StreamedSnapshot(snapshot, status_file, statuses_present)
    except Exception:
        if status_file is not None:
            status_file.close()
        raise
    finally:
        # The UTF-8 decoded copy is intentionally discarded before SQLite staging.
        del text
