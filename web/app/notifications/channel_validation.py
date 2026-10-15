from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

from .rules import RuleValidationError, validate_template
from .security import DestinationRejected, validate_destination_url


class ChannelValidationError(ValueError):
    pass


HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")
BLOCKED_HEADERS = {"host", "content-length", "transfer-encoding", "connection", "proxy-authorization", "proxy-authenticate", "cookie", "set-cookie"}


def _only_keys(config: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(config) - allowed
    if unknown:
        raise ChannelValidationError(f"Unknown provider configuration field: {sorted(unknown)[0]}")


def _validate_destination(url: str) -> None:
    try:
        validate_destination_url(url)
    except DestinationRejected as exc:
        raise ChannelValidationError(str(exc)) from exc


def _string(value: Any, name: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ChannelValidationError(f"{name} must be a non-empty string of at most {maximum} characters")
    return value.strip()


def validate_channel_configuration(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ChannelValidationError("Provider configuration must be an object")
    if provider == "telegram":
        _only_keys(config, {"chat_id"})
        chat_id = config.get("chat_id")
        if isinstance(chat_id, bool) or not isinstance(chat_id, (str, int)):
            raise ChannelValidationError("Telegram chat_id must be a string or integer")
        chat_id = str(chat_id).strip()
        if not chat_id or len(chat_id) > 128:
            raise ChannelValidationError("Telegram chat_id is invalid")
        return {"chat_id": chat_id}
    if provider == "gotify":
        _only_keys(config, {"base_url", "priority"})
        base_url = _string(config.get("base_url"), "base_url", 2048).rstrip("/")
        _validate_destination(base_url)
        priority = config.get("priority", 5)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 0 <= priority <= 10:
            raise ChannelValidationError("Gotify priority must be an integer from 0 to 10")
        return {"base_url": base_url, "priority": priority}
    if provider == "ntfy":
        _only_keys(config, {"base_url", "topic", "priority"})
        base_url = _string(config.get("base_url"), "base_url", 2048).rstrip("/")
        _validate_destination(base_url)
        topic = _string(config.get("topic"), "topic", 128)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", topic):
            raise ChannelValidationError("ntfy topic may contain only letters, digits, underscore and hyphen")
        priority = config.get("priority", 3)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 5:
            raise ChannelValidationError("ntfy priority must be an integer from 1 to 5")
        return {"base_url": base_url, "topic": topic, "priority": priority}
    if provider == "webhook":
        _only_keys(config, {"url", "method", "content_type", "header_names", "json_template"})
        url = _string(config.get("url"), "url", 2048)
        _validate_destination(url)
        method = config.get("method", "POST")
        if method != "POST":
            raise ChannelValidationError("Webhook method must be POST")
        content_type = config.get("content_type", "application/json")
        if content_type != "application/json":
            raise ChannelValidationError("Webhook Content-Type must be application/json")
        header_names = config.get("header_names", [])
        if not isinstance(header_names, list) or len(header_names) > 20:
            raise ChannelValidationError("header_names must be a list with at most 20 entries")
        normalized_names = []
        seen = set()
        for name in header_names:
            if not isinstance(name, str) or not HEADER_NAME.fullmatch(name) or name.casefold() in BLOCKED_HEADERS:
                raise ChannelValidationError("Webhook contains an invalid or restricted header name")
            if name.casefold() in seen:
                raise ChannelValidationError("Webhook header names must be unique")
            seen.add(name.casefold())
            normalized_names.append(name)
        template = config.get("json_template", {
            "event_type": "{event_type}", "host": "{host}", "service": "{service}", "state": "{state}",
            "source": "{source}", "timestamp": "{timestamp}",
        })
        try:
            encoded = json.dumps(template, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ChannelValidationError("Webhook JSON template must be JSON serializable") from exc
        if len(encoded.encode("utf-8")) > 8192:
            raise ChannelValidationError("Webhook JSON template exceeds 8 KiB")
        _validate_template_nodes(template)
        return {"url": url, "method": "POST", "content_type": "application/json",
                "header_names": normalized_names, "json_template": template}
    raise ChannelValidationError("Unsupported notification provider")


def _validate_template_nodes(value: Any) -> None:
    if isinstance(value, dict):
        for child in value.values():
            _validate_template_nodes(child)
    elif isinstance(value, list):
        for child in value:
            _validate_template_nodes(child)
    elif isinstance(value, str):
        try:
            validate_template(value, maximum=2048)
        except RuleValidationError as exc:
            raise ChannelValidationError(str(exc)) from exc
    elif value is not None and not isinstance(value, (int, float, bool)):
        raise ChannelValidationError("Webhook JSON template contains an unsupported value")


def validate_secrets(provider: str, values: Any, public_config: dict[str, Any]) -> dict[str, Any]:
    if values is None:
        return {}
    if not isinstance(values, dict):
        raise ChannelValidationError("secrets must be an object")
    if provider == "telegram":
        _only_keys(values, {"bot_token"})
        token = values.get("bot_token")
        if token is None:
            return {}
        token = _string(token, "bot_token", 256)
        if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,128}", token):
            raise ChannelValidationError("Telegram bot token format is invalid")
        return {"bot_token": token}
    if provider == "gotify":
        _only_keys(values, {"app_token"})
        token = values.get("app_token")
        return {} if token is None else {"app_token": _string(token, "app_token", 512)}
    if provider == "ntfy":
        _only_keys(values, {"access_token", "username", "password"})
        result = {}
        for key in ("access_token", "username", "password"):
            if values.get(key) is not None:
                result[key] = _string(values[key], key, 512)
        if result.get("access_token") and ({"username", "password"} & result.keys()):
            raise ChannelValidationError("Use either an ntfy access token or username/password, not both")
        if ("username" in result) != ("password" in result):
            raise ChannelValidationError("ntfy username and password must be configured together")
        return result
    if provider == "webhook":
        _only_keys(values, {"headers"})
        headers = values.get("headers", {})
        if not isinstance(headers, dict):
            raise ChannelValidationError("Webhook secret headers must be an object")
        allowed = {name.casefold(): name for name in public_config.get("header_names", [])}
        result = {}
        for name, value in headers.items():
            if not isinstance(name, str) or name.casefold() not in allowed:
                raise ChannelValidationError("Webhook secret uses a header not listed in header_names")
            normalized = allowed[name.casefold()]
            result[normalized] = _string(value, f"header {normalized}", 2048)
        return {"headers": result}
    raise ChannelValidationError("Unsupported notification provider")
