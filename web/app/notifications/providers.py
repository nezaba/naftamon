from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
from typing import Any

import httpx
from starlette.concurrency import run_in_threadpool

from .rules import render_template
from .security import DestinationRejected, ResolvedDestination, prepare_destination_url


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    transient: bool
    error: str | None = None
    status_code: int | None = None
    retry_after_seconds: int | None = None


class ProviderConfigurationError(ValueError):
    pass


class PinnedAddressTransport(httpx.AsyncBaseTransport):
    """Connect to the checked IP while retaining the destination hostname."""

    def __init__(self, destination: ResolvedDestination, inner: httpx.AsyncBaseTransport | None = None):
        self.destination = destination
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0, trust_env=False)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        destination = self.destination
        if destination.pinned_ip:
            scheme = request.url.scheme
            port = request.url.port
            default_port = 443 if scheme == "https" else 80
            authority = destination.host if port is None or port == default_port else f"{destination.host}:{port}"
            request.url = request.url.copy_with(host=destination.pinned_ip)
            request.headers["Host"] = authority
            if scheme == "https":
                # httpcore uses this for both TLS SNI and certificate hostname
                # verification, while the TCP connection uses the IP URL host.
                request.extensions["sni_hostname"] = destination.host
        return await self.inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def _render_json_template(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {str(k): _render_json_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_json_template(item, context) for item in value]
    if isinstance(value, str):
        return render_template(value, context)
    if value is None or isinstance(value, (int, float, bool)):
        return value
    raise ProviderConfigurationError("Webhook JSON template contains an unsupported value")


def _retry_after(header: str | None) -> int | None:
    if not header:
        return None
    try:
        return max(0, min(21600, int(header.strip())))
    except ValueError:
        try:
            when = parsedate_to_datetime(header)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, min(21600, int((when - datetime.now(timezone.utc)).total_seconds())))
        except (TypeError, ValueError, OverflowError):
            return None


def _response_result(response: httpx.Response) -> DeliveryResult:
    if 200 <= response.status_code < 300:
        return DeliveryResult(ok=True, transient=False, status_code=response.status_code)
    if response.status_code == 429:
        return DeliveryResult(False, True, "Provider rate limited the request (HTTP 429)", 429, _retry_after(response.headers.get("Retry-After")))
    if 500 <= response.status_code <= 599:
        return DeliveryResult(False, True, f"Provider server error (HTTP {response.status_code})", response.status_code)
    return DeliveryResult(False, False, f"Provider rejected the request (HTTP {response.status_code})", response.status_code)


async def send_notification(
    provider: str,
    config: dict[str, Any],
    secrets: dict[str, Any],
    context: dict[str, Any],
    *,
    subject_template: str,
    body_template: str,
    timeout_seconds: int = 10,
    delivery_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> DeliveryResult:
    title = render_template(subject_template, context)
    body = render_template(body_template, context)
    headers: dict[str, str] = {}
    auth = None
    json_body: Any
    url: str
    destination: ResolvedDestination

    try:
        if provider == "telegram":
            token = secrets.get("bot_token")
            chat_id = config.get("chat_id")
            if not token or not chat_id:
                raise ProviderConfigurationError("Telegram bot token and chat ID are required")
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            destination = await run_in_threadpool(prepare_destination_url, url)
            json_body = {"chat_id": str(chat_id), "text": body or title, "disable_web_page_preview": True}
        elif provider == "gotify":
            token = secrets.get("app_token")
            if not token:
                raise ProviderConfigurationError("Gotify application token is required")
            url = str(config.get("base_url", "")).rstrip("/") + "/message"
            destination = await run_in_threadpool(prepare_destination_url, url)
            headers["X-Gotify-Key"] = str(token)
            json_body = {"title": title, "message": body, "priority": int(config.get("priority", 5))}
        elif provider == "ntfy":
            base_url = str(config.get("base_url", "")).rstrip("/")
            topic = str(config.get("topic", ""))
            # ntfy's JSON publish API accepts the topic in the body and posts
            # to the server root. Keeping the topic out of the URL also avoids
            # accidental exposure in access logs.
            url = base_url
            destination = await run_in_threadpool(prepare_destination_url, url)
            token = secrets.get("access_token")
            username, password = secrets.get("username"), secrets.get("password")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            elif username or password:
                if not username or not password:
                    raise ProviderConfigurationError("ntfy username and password must be configured together")
                auth = (str(username), str(password))
            json_body = {"topic": topic, "title": title, "message": body, "priority": int(config.get("priority", 3))}
        elif provider == "webhook":
            url = str(config.get("url", ""))
            destination = await run_in_threadpool(prepare_destination_url, url)
            header_names = config.get("header_names", [])
            secret_headers = secrets.get("headers", {})
            if not isinstance(secret_headers, dict):
                raise ProviderConfigurationError("Webhook headers are invalid")
            for name in header_names:
                if name in secret_headers:
                    headers[name] = str(secret_headers[name])
            template = config.get("json_template", {"title": "{event_type}", "message": "{host} {service} {state}"})
            json_body = _render_json_template(template, context)
            headers["Content-Type"] = "application/json"
        else:
            raise ProviderConfigurationError("Unsupported notification provider")

        if delivery_id:
            headers["Idempotency-Key"] = delivery_id
        request_transport = transport or PinnedAddressTransport(destination)
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=request_transport,
        ) as client:
            response = await client.post(url, headers=headers, json=json_body, auth=auth)
        return _response_result(response)
    except DestinationRejected as exc:
        return DeliveryResult(False, False, str(exc))
    except ProviderConfigurationError as exc:
        return DeliveryResult(False, False, str(exc))
    except httpx.TimeoutException:
        return DeliveryResult(False, True, "Provider request timed out")
    except httpx.RequestError:
        return DeliveryResult(False, True, "Provider connection failed")
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return DeliveryResult(False, False, "Provider configuration is invalid")
