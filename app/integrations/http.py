"""Shared HTTP plumbing for adapters: timeouts, error classification, bounded retries.

Every REST adapter (Rocketlane, Vapi) sends requests through `HttpService.request`:

- explicit connect/read timeouts on every call;
- responses mapped to domain errors: 408/425/429/5xx → RetryableError, other 4xx →
  PermanentError, so no vendor exception leaks past the adapter;
- bounded in-request retries with exponential backoff and jitter, honouring
  `Retry-After` (capped, so a provider can't park us for an hour);
- non-idempotent requests (creates) are retried only when the request provably did
  not reach the server. Anything ambiguous raises OutcomeUnknownError so the caller
  checks for its marker before trying again;
- a per-service semaphore caps in-flight requests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from typing import Any, Final, Self

import httpx

from app.config import Settings
from app.errors import OutcomeUnknownError, PermanentError, RetryableError
from app.integrations.retry import RetryPolicy, Sleep, run_with_retries

RETRYABLE_STATUS: Final = frozenset({408, 425, 429, 500, 502, 503, 504})
# Errors raised before any byte of the request reached the server: always safe to retry.
_NOT_SENT: Final = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
MAX_ERROR_BODY_CHARS: Final = 300

# Validates a successful response; raises a domain error if its body reports a failure.
ResponseCheck = Callable[[httpx.Response], None]


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


def raise_for_status(response: httpx.Response, *, service: str) -> None:
    """Map a non-2xx response to a domain error."""
    if response.is_success:
        return
    code = response.status_code
    message = f"{service}: HTTP {code} on {response.request.method} {response.request.url.path}"
    if code in RETRYABLE_STATUS:
        raise RetryableError(
            message, retry_after=parse_retry_after(response.headers.get("retry-after"))
        )
    # Provider error bodies explain 4xx failures ("field X is required"). They never
    # contain our credentials, but cap the size so a full HTML page can't flood the log.
    raise PermanentError(f"{message}: {response.text[:MAX_ERROR_BODY_CHARS]}")


class HttpService:
    """One external REST API: a configured client, its retry policy and a concurrency cap."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        name: str,
        policy: RetryPolicy,
        max_concurrency: int,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.name = name
        self._client = client
        self._policy = policy
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._sleep = sleep

    @classmethod
    def create(
        cls,
        settings: Settings,
        *,
        name: str,
        base_url: str,
        headers: Mapping[str, str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> Self:
        timeout = httpx.Timeout(
            settings.http_read_timeout_seconds, connect=settings.http_connect_timeout_seconds
        )
        client = httpx.AsyncClient(
            base_url=base_url, headers=dict(headers), timeout=timeout, transport=transport
        )
        return cls(
            client,
            name=name,
            policy=RetryPolicy.from_settings(settings),
            max_concurrency=settings.http_max_concurrency,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        url: str,
        *,
        idempotent: bool = True,
        check: ResponseCheck | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send with retries. Set `idempotent=False` for creates (see module docstring).

        `check` validates a 2xx response for APIs that report errors in the body (Slack's
        `ok: false`). It runs inside the retry loop, so a RetryableError it raises is
        retried like any other.
        """
        send = partial(self._send_once, method, url, idempotent=idempotent, check=check, **kwargs)
        return await run_with_retries(
            send, service=self.name, policy=self._policy, sleep=self._sleep
        )

    async def _send_once(
        self,
        method: str,
        url: str,
        *,
        idempotent: bool,
        check: ResponseCheck | None,
        **kwargs: Any,
    ) -> httpx.Response:
        async with self._semaphore:
            try:
                response = await self._client.request(method, url, **kwargs)
            except _NOT_SENT as exc:
                raise RetryableError(f"{self.name}: {type(exc).__name__}") from exc
            except httpx.TransportError as exc:
                # Sent, but the response was lost (read timeout, dropped connection).
                error = f"{self.name}: {type(exc).__name__} after sending {method} {url}"
                if idempotent:
                    raise RetryableError(error) from exc
                raise OutcomeUnknownError(error) from exc
        try:
            raise_for_status(response, service=self.name)
        except RetryableError as exc:
            # 429 means the provider refused the request, so even a create is safe to
            # repeat. A 5xx or 408 on a create may still have created it.
            if not idempotent and response.status_code != 429:
                raise OutcomeUnknownError(str(exc), retry_after=exc.retry_after) from exc
            raise
        if check is not None:
            check(response)
        return response
