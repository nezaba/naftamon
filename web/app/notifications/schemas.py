from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChannelWrite(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    provider: Literal["telegram", "gotify", "ntfy", "webhook"]
    config: dict[str, Any]
    secrets: dict[str, Any] | None = None
    enabled: bool = False
    timeout_seconds: int = Field(default=10, ge=1, le=60)
    rate_limit_seconds: int = Field(default=1, ge=0, le=3600)


class RuleWrite(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    enabled: bool = False
    conditions: dict[str, Any]
    events: list[str] = Field(default_factory=lambda: ["opened", "state_changed"])
    channel_ids: list[int] = Field(default_factory=list, max_length=100)
    cooldown_seconds: int = Field(default=0, ge=0, le=31_536_000)
    reminder_interval_seconds: int | None = Field(default=None, gt=0, le=31_536_000)
    subject_template: str = Field(default="NaftaMon {event_type}: {state} {host}", min_length=1, max_length=500)
    body_template: str = Field(
        default="{event_type}: {host} {service} {state} ({server}, {source})",
        min_length=1,
        max_length=2000,
    )


class NotificationSettingsWrite(StrictModel):
    enabled: bool
    history_retention_days: int = Field(ge=0, le=3650)
