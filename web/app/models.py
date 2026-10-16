from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator


def validate_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a UTC offset")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ServerStatus(StrictModel):
    server: str = Field(min_length=1, max_length=255)
    status: Literal["ok", "error", "pending"]


class Alert(StrictModel):
    server: str = Field(min_length=1, max_length=255)
    backend: str = Field(max_length=255)
    host: str = Field(min_length=1, max_length=255)
    service: str = Field(max_length=255)
    type: Literal["host", "service"]
    state: Literal["UNKNOWN", "WARNING", "CRITICAL", "UNREACHABLE", "DOWN"]
    hard: StrictBool
    acknowledged: StrictBool
    downtime: StrictBool
    last_check: str = Field(max_length=64)
    last_change: str = Field(max_length=64)
    # model_fields_set distinguishes a legacy omission from an explicit empty value.
    status_information: str = ""

    _check_timestamp = field_validator("last_check", "last_change")(validate_timestamp)

    @model_validator(mode="after")
    def validate_alert_kind(self) -> "Alert":
        if self.type == "host" and self.service:
            raise ValueError("host alerts must have an empty service")
        if self.type == "service" and not self.service:
            raise ValueError("service alerts must have a service name")
        return self


class OkStatus(StrictModel):
    server: str = Field(min_length=1, max_length=255)
    backend: str = Field(max_length=255)
    host: str = Field(min_length=1, max_length=255)
    service: str = Field(max_length=255)
    type: Literal["host", "service"]
    state: Literal["UP"]
    hard: StrictBool
    acknowledged: StrictBool
    downtime: StrictBool
    last_check: str = Field(max_length=64)
    last_change: str = Field(max_length=64)
    status_information: str = ""

    _check_timestamp = field_validator("last_check", "last_change")(validate_timestamp)

    @model_validator(mode="after")
    def validate_status_kind(self) -> "OkStatus":
        if self.type == "host" and self.service:
            raise ValueError("host statuses must have an empty service")
        if self.type == "service" and not self.service:
            raise ValueError("service statuses must have a service name")
        return self


class Snapshot(StrictModel):
    timestamp: str = Field(max_length=64)
    source: Literal["NaftaMon"]
    complete: StrictBool
    servers: list[ServerStatus] = Field(max_length=1000)
    alerts: list[Alert] = Field(max_length=200000)
    # None distinguishes a legacy snapshot from a new authoritative empty list.
    ok_statuses: list[OkStatus] | None = Field(default=None, max_length=200000)

    _check_timestamp = field_validator("timestamp")(validate_timestamp)

    @field_validator("ok_statuses", mode="before")
    @classmethod
    def reject_null_ok_statuses(cls, value):
        if value is None:
            raise ValueError("ok_statuses must be an array when provided")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> "Snapshot":
        server_status = {entry.server: entry.status for entry in self.servers}
        if len(server_status) != len(self.servers):
            raise ValueError("servers must have unique names")
        expected_complete = bool(self.servers) and all(s.status == "ok" for s in self.servers)
        if self.complete != expected_complete:
            raise ValueError("complete must be true only when at least one server exists and every server is ok")
        for alert in self.alerts:
            if server_status.get(alert.server) != "ok":
                raise ValueError("alerts may only be included for a server with status ok")
        for status in self.ok_statuses or []:
            if server_status.get(status.server) != "ok":
                raise ValueError("ok_statuses may only be included for a server with status ok")
        alert_keys = set()
        for alert in self.alerts:
            key = (alert.server, alert.backend, alert.type, alert.host, alert.service)
            if key in alert_keys:
                raise ValueError("alerts must have unique composite identities")
            alert_keys.add(key)
        status_keys = set()
        for status in self.ok_statuses or []:
            key = (status.server, status.backend, status.type, status.host, status.service)
            if key in status_keys:
                raise ValueError("ok_statuses must have unique composite identities")
            if key in alert_keys:
                raise ValueError("alerts and ok_statuses must not share composite identities")
            status_keys.add(key)
        return self


class TestMessage(StrictModel):
    type: Literal["test"]
    test: Literal[True]
    source: Literal["NaftaMon"]
    timestamp: str = Field(max_length=64)
    message: str = Field(min_length=1, max_length=512)

    _check_timestamp = field_validator("timestamp")(validate_timestamp)
