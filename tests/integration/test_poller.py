"""InboxPoller with the fake mailbox: ordering, labelling, backoff, auth stop, poison messages."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.config import Settings
from app.db import Database
from app.errors import PermanentError, RetryableError, ValidationFailedError
from app.integrations.fakes import FakeMailbox
from app.integrations.gmail import GmailAuthError
from app.models import AuditLog, Deal
from app.orchestrator import poller as poller_module
from app.orchestrator.poller import MAX_MESSAGE_ATTEMPTS, InboxPoller
from app.orchestrator.workflow import InboundResult
from app.ports import InboundEmail
from tests.factories import T0, inbound_email


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def poller(mailbox: FakeMailbox, db: Database, settings: Settings, clock: Clock) -> InboxPoller:
    return InboxPoller(mailbox, db, settings, clock=clock)


async def _deal_message_ids(db: Database) -> list[str]:
    async with db.session() as session:
        return [d.gmail_message_id for d in (await session.exec(select(Deal))).all()]


async def _audit_actions(db: Database) -> list[str]:
    async with db.session() as session:
        return [r.action for r in (await session.exec(select(AuditLog))).all()]


async def test_processes_oldest_first_and_labels(
    poller: InboxPoller, mailbox: FakeMailbox, db: Database
) -> None:
    mailbox.deliver(inbound_email("late", minutes=10))
    mailbox.deliver(inbound_email("early", minutes=1))

    report = await poller.poll_once()

    assert report is not None
    assert report.listed == 2
    assert report.outcomes == {"created": 2}
    assert mailbox.called("fetch") == ["early", "late"]
    assert mailbox.processed == {"early", "late"}
    assert sorted(await _deal_message_ids(db)) == ["early", "late"]
    assert poller.status.last_success_at == poller.status.last_poll_at


async def test_second_poll_does_nothing(poller: InboxPoller, mailbox: FakeMailbox) -> None:
    mailbox.deliver(inbound_email("m1"))
    await poller.poll_once()
    report = await poller.poll_once()
    assert report is not None
    assert report.listed == 0


async def test_label_failure_is_harmless(
    poller: InboxPoller, mailbox: FakeMailbox, db: Database
) -> None:
    mailbox.deliver(inbound_email("m1"))
    mailbox.fail("mark_processed", RetryableError("gmail 503"))

    first = await poller.poll_once()
    second = await poller.poll_once()  # sees it again: deduped, then labelled

    assert first is not None
    assert first.outcomes == {"created": 1}
    assert second is not None
    assert second.outcomes == {"duplicate": 1}
    assert await _deal_message_ids(db) == ["m1"]
    assert mailbox.processed == {"m1"}


async def test_list_failures_back_off_then_recover(
    poller: InboxPoller, mailbox: FakeMailbox, clock: Clock, settings: Settings
) -> None:
    mailbox.fail("list_new_message_ids", RetryableError("a"), RetryableError("b"))

    assert await poller.poll_once() is None
    assert poller.status.consecutive_failures == 1
    first_wait = settings.gmail_poll_seconds * 2

    clock.advance(first_wait - 1)
    assert await poller.poll_once() is None  # still backing off: no call made
    assert len(mailbox.called("list_new_message_ids")) == 1

    clock.advance(1)
    assert await poller.poll_once() is None  # second failure, longer wait
    assert poller.status.consecutive_failures == 2
    assert poller.status.next_poll_after == clock.now + timedelta(
        seconds=settings.gmail_poll_seconds * 4
    )

    clock.advance(settings.gmail_max_poll_backoff_seconds)
    assert await poller.poll_once() is not None
    assert poller.status.consecutive_failures == 0
    assert poller.status.last_error is None
    assert poller.status.healthy


async def test_backoff_is_capped(
    poller: InboxPoller, mailbox: FakeMailbox, settings: Settings, clock: Clock
) -> None:
    for _ in range(10):
        mailbox.fail("list_new_message_ids", RetryableError("down"))
        await poller.poll_once()
        assert poller.status.next_poll_after is not None
        assert poller.status.next_poll_after - clock.now <= timedelta(
            seconds=settings.gmail_max_poll_backoff_seconds
        )
        clock.now = poller.status.next_poll_after
    assert not poller.status.healthy


async def test_auth_failure_stops_polling_and_alerts_once(
    mailbox: FakeMailbox, db: Database, settings: Settings, clock: Clock
) -> None:
    alerts: list[str] = []

    async def alert(reason: str) -> None:
        alerts.append(reason)

    poller = InboxPoller(mailbox, db, settings, clock=clock, on_auth_failure=alert)
    mailbox.fail("list_new_message_ids", GmailAuthError("invalid_grant"))

    assert await poller.poll_once() is None
    clock.advance(3600)
    assert await poller.poll_once() is None

    assert alerts == ["invalid_grant"]
    assert len(mailbox.called("list_new_message_ids")) == 1
    assert poller.status.auth_failed
    assert not poller.status.healthy


async def test_unusable_message_is_abandoned_and_labelled(
    poller: InboxPoller, mailbox: FakeMailbox, db: Database
) -> None:
    mailbox.deliver(inbound_email("bad"))
    mailbox.fail("fetch", ValidationFailedError("no sender"))

    report = await poller.poll_once()

    assert report is not None
    assert report.outcomes == {"unusable": 1}
    assert mailbox.processed == {"bad"}
    assert await _deal_message_ids(db) == []
    assert await _audit_actions(db) == ["abandon_email"]


async def test_failing_message_is_abandoned_after_max_attempts(
    poller: InboxPoller, mailbox: FakeMailbox, db: Database
) -> None:
    mailbox.deliver(inbound_email("flaky"))
    mailbox.deliver(inbound_email("fine", minutes=1))
    mailbox.break_message("flaky", PermanentError("404"))

    outcomes: list[dict[str, int]] = []
    for _ in range(MAX_MESSAGE_ATTEMPTS):
        report = await poller.poll_once()
        assert report is not None
        outcomes.append(dict(report.outcomes))

    # The bad message never blocks the good one.
    assert outcomes[0] == {"failed": 1, "created": 1}
    assert outcomes[-1] == {"abandoned": 1}
    assert mailbox.processed == {"flaky", "fine"}
    assert await _audit_actions(db) == ["receive_email", "abandon_email"]


async def test_unexpected_handler_error_is_isolated(
    poller: InboxPoller,
    mailbox: FakeMailbox,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = poller_module.handle_inbound

    async def explode_on_first(
        db: Database, settings: Settings, email: InboundEmail
    ) -> InboundResult:
        if email.message_id == "boom":
            raise RuntimeError("bug")
        return await real(db, settings, email)

    monkeypatch.setattr(poller_module, "handle_inbound", explode_on_first)
    mailbox.deliver(inbound_email("boom"))
    mailbox.deliver(inbound_email("ok", minutes=1))

    report = await poller.poll_once()

    assert report is not None
    assert report.outcomes == {"failed": 1, "created": 1}
    assert "boom" not in mailbox.processed  # retried next tick
    assert poller.status.healthy  # one bad message isn't an inbox outage
