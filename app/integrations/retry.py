"""Bounded in-request retries shared by every adapter (REST via httpx, Gmail via its SDK).

Only `RetryableError` is retried, and never `OutcomeUnknownError`: an ambiguous create
has to be checked for its marker before anyone repeats it. Waits use exponential backoff
with jitter, or the provider's `Retry-After` when it gave one, capped at `max_seconds`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Self

import structlog
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.config import Settings
from app.errors import OutcomeUnknownError, RetryableError

log = structlog.get_logger(__name__)

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int
    base_seconds: float
    max_seconds: float

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(
            attempts=settings.http_max_attempts,
            base_seconds=settings.http_backoff_base_seconds,
            max_seconds=settings.http_backoff_max_seconds,
        )


def is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RetryableError) and not isinstance(exc, OutcomeUnknownError)


async def run_with_retries[T](
    operation: Callable[[], Awaitable[T]],
    *,
    service: str,
    policy: RetryPolicy,
    sleep: Sleep = asyncio.sleep,
) -> T:
    """Run `operation`, retrying retryable domain errors; re-raise the last error."""
    backoff = wait_exponential_jitter(multiplier=policy.base_seconds, max=policy.max_seconds)

    def wait(state: RetryCallState) -> float:
        exc = state.outcome.exception() if state.outcome is not None else None
        if isinstance(exc, RetryableError) and exc.retry_after is not None:
            return min(exc.retry_after, policy.max_seconds)
        return backoff(state)

    def log_retry(state: RetryCallState) -> None:
        exc = state.outcome.exception() if state.outcome is not None else None
        log.warning(
            "retry",
            service=service,
            attempt=state.attempt_number,
            wait_s=round(state.upcoming_sleep, 2),
            error=str(exc),
        )

    retrying = AsyncRetrying(
        stop=stop_after_attempt(policy.attempts),
        wait=wait,
        retry=retry_if_exception(is_retryable),
        sleep=sleep,
        before_sleep=log_retry,
        reraise=True,
    )
    return await retrying(operation)
