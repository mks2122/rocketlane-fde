"""Inbox poller: list new emails, hand each to `handle_inbound`, then label it processed.

Failure handling:
- List fails transiently → skip this tick; ticks back off exponentially
  (poll interval * 2^failures, capped) until a poll succeeds.
- Our authorisation is gone → stop polling, mark the inbox unhealthy, alert once.
  Polling resumes only after re-auth and a restart; retrying a revoked token is pointless.
- One message keeps failing → retried on later ticks up to MAX_MESSAGE_ATTEMPTS, then
  audited as abandoned and labelled, so it can't block the inbox forever.
- Unusable message (no valid sender, broken timestamp) → audited and labelled immediately.

The label is only an optimisation. Deal creation is idempotent on the Gmail message id,
so a message that was handled but not labelled is a harmless duplicate next tick.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

import structlog

from app.config import Settings
from app.db import Database
from app.errors import AuthorizationError, IntegrationError, ValidationFailedError
from app.models import utcnow
from app.orchestrator.workflow import ACTOR_INBOX, handle_inbound
from app.ports import Mailbox
from app.services.audit import record

log = structlog.get_logger(__name__)

MAX_MESSAGE_ATTEMPTS: Final = 3
UNHEALTHY_AFTER_FAILURES: Final = 3

Clock = Callable[[], datetime]
AuthFailureHook = Callable[[str], Awaitable[None]]


@dataclass
class PollerStatus:
    last_poll_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    auth_failed: bool = False
    consecutive_failures: int = 0
    next_poll_after: datetime | None = None

    @property
    def healthy(self) -> bool:
        return not self.auth_failed and self.consecutive_failures < UNHEALTHY_AFTER_FAILURES


@dataclass
class PollReport:
    listed: int = 0
    outcomes: Counter[str] = field(default_factory=Counter[str])


class InboxPoller:
    def __init__(
        self,
        mailbox: Mailbox,
        db: Database,
        settings: Settings,
        *,
        clock: Clock = utcnow,
        on_auth_failure: AuthFailureHook | None = None,
    ) -> None:
        self._mailbox = mailbox
        self._db = db
        self._settings = settings
        self._clock = clock
        self._on_auth_failure = on_auth_failure
        self.status = PollerStatus()
        # Throttle for poison messages only. Losing it on restart just allows a few more
        # attempts, so it doesn't need to live in the database.
        self._message_failures: Counter[str] = Counter()

    async def poll_once(self) -> PollReport | None:
        """One tick. Returns None when the tick was skipped (backoff or auth failure)."""
        now = self._clock()
        if self.status.auth_failed:
            return None
        if self.status.next_poll_after is not None and now < self.status.next_poll_after:
            return None
        self.status.last_poll_at = now

        try:
            message_ids = await self._mailbox.list_new_message_ids()
            report = PollReport(listed=len(message_ids))
            for message_id in message_ids:
                report.outcomes[await self._process(message_id)] += 1
        except AuthorizationError as exc:
            await self._stop_on_auth_failure(exc)
            return None
        except IntegrationError as exc:
            self._back_off(now, exc)
            return None

        self.status.last_success_at = now
        self.status.last_error = None
        self.status.consecutive_failures = 0
        self.status.next_poll_after = None
        if message_ids:
            log.info("inbox_polled", listed=report.listed, **report.outcomes)
        return report

    async def _process(self, message_id: str) -> str:
        try:
            email = await self._mailbox.fetch(message_id)
        except ValidationFailedError as exc:
            await self._abandon(message_id, reason=f"unusable message: {exc}")
            return "unusable"
        except AuthorizationError:
            raise
        except IntegrationError as exc:
            return await self._count_failure(message_id, exc)

        try:
            outcome: str = await handle_inbound(self._db, self._settings, email)
        # Isolation boundary: an unexpected bug on one message must not stop every
        # later message from being processed. It is logged with its traceback, retried
        # on later ticks, and abandoned (audited) after MAX_MESSAGE_ATTEMPTS.
        except Exception as exc:
            log.exception("inbound_handler_failed", gmail_message_id=message_id)
            return await self._count_failure(message_id, exc)

        self._message_failures.pop(message_id, None)
        await self._mark_processed(message_id)
        return outcome

    async def _count_failure(self, message_id: str, exc: Exception) -> str:
        self._message_failures[message_id] += 1
        attempts = self._message_failures[message_id]
        log.warning(
            "inbound_message_failed",
            gmail_message_id=message_id,
            attempt=attempts,
            error=f"{type(exc).__name__}: {exc}",
        )
        if attempts >= MAX_MESSAGE_ATTEMPTS:
            await self._abandon(
                message_id, reason=f"failed {attempts} times; last error {type(exc).__name__}"
            )
            return "abandoned"
        return "failed"

    async def _abandon(self, message_id: str, *, reason: str) -> None:
        log.error("inbound_message_abandoned", gmail_message_id=message_id, reason=reason)
        async with self._db.session() as session:
            record(
                session,
                deal_id=None,
                actor=ACTOR_INBOX,
                action="abandon_email",
                input={"gmail_message_id": message_id},
                rationale=reason,
            )
        self._message_failures.pop(message_id, None)
        await self._mark_processed(message_id)

    async def _mark_processed(self, message_id: str) -> None:
        try:
            await self._mailbox.mark_processed(message_id)
        except AuthorizationError:
            raise
        except IntegrationError as exc:
            # Safe to leave: the next tick sees it again and handle_inbound dedupes.
            log.warning("inbox_label_failed", gmail_message_id=message_id, error=str(exc))

    def _back_off(self, now: datetime, exc: IntegrationError) -> None:
        status = self.status
        status.consecutive_failures += 1
        status.last_error = f"{type(exc).__name__}: {exc}"
        delay = min(
            self._settings.gmail_poll_seconds * 2**status.consecutive_failures,
            self._settings.gmail_max_poll_backoff_seconds,
        )
        status.next_poll_after = now + timedelta(seconds=delay)
        log.warning(
            "inbox_poll_failed",
            failures=status.consecutive_failures,
            retry_in_s=delay,
            error=status.last_error,
        )

    async def _stop_on_auth_failure(self, exc: AuthorizationError) -> None:
        self.status.auth_failed = True
        self.status.last_error = str(exc)
        log.error("inbox_auth_failed_polling_stopped", error=str(exc))
        if self._on_auth_failure is not None:
            await self._on_auth_failure(str(exc))
