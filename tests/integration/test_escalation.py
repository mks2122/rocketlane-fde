"""Escalation notices: delivery across channels, retries, idempotency, and the notice text."""

from __future__ import annotations

from typing import Any

import pytest
from sqlmodel import select

from app.config import Settings
from app.db import Database
from app.domain import DealStatus
from app.errors import AuthorizationError, PermanentError, RetryableError
from app.integrations.fakes import (
    FakeChatWorkspace,
    FakeJobScheduler,
    FakeMailbox,
    FakeTierVerifier,
    FakeVoiceCaller,
)
from app.models import AuditLog, CallAttempt, Deal
from app.orchestrator.escalation import (
    NOTICE_JOB,
    escalate,
    queue_escalation_notice,
    send_escalation_notice,
    send_system_alert,
)
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.services.escalation_notice import compose_notice

ESCALATION_EMAIL = "priya.cs@novacrm-demo.com"


@pytest.fixture
def jobs() -> FakeJobScheduler:
    return FakeJobScheduler()


@pytest.fixture
async def chat() -> FakeChatWorkspace:
    workspace = FakeChatWorkspace()
    await workspace.create_channel("cs-escalations")
    workspace.calls.clear()
    return workspace


@pytest.fixture
def rt(
    settings: Settings,
    db: Database,
    jobs: FakeJobScheduler,
    mailbox: FakeMailbox,
    chat: FakeChatWorkspace,
) -> Runtime:
    configured = settings.model_copy(
        update={
            "escalation_email": ESCALATION_EMAIL,
            "escalation_notice_retry_delays_seconds": [300, 900],
        }
    )
    return Runtime(
        settings=configured,
        db=db,
        voice=FakeVoiceCaller(),
        tier_verifier=FakeTierVerifier(),
        jobs=jobs,
        mailbox=mailbox,
        chat=chat,
    )


async def _escalated_deal(rt: Runtime, reason: str = "tier not confirmed after 3 attempts") -> Deal:
    async with rt.db.session() as session:
        deal = Deal(
            gmail_message_id="m1",
            gmail_thread_id="t1",
            sender_email="ravi@x.com",
            customer_company="Acme Corp",
            ae_name="Ravi Kumar",
            ae_email="ravi.ae@novacrm-demo.com",
            ae_phone="+919876543210",
        )
        session.add(deal)
        await session.flush()
        deal = await transition(session, deal, DealStatus.AWAITING_TIER, actor="t", rationale="t")
        return await escalate(session, deal, actor="voice_agent", reason=reason)


async def _deal(rt: Runtime, deal_id: str) -> Deal:
    async with rt.db.session() as session:
        found = await session.get(Deal, deal_id)
    assert found is not None
    return found


async def _notice_rows(rt: Runtime) -> list[AuditLog]:
    async with rt.db.session() as session:
        rows = await session.exec(select(AuditLog).where(AuditLog.action == "escalation_notice"))
        return list(rows.all())


def _slack_texts(chat: FakeChatWorkspace) -> list[str]:
    return [text for msgs in chat.messages.values() for text in msgs]


# --- delivery ----------------------------------------------------------------------


async def test_notice_goes_to_slack_and_email(
    rt: Runtime, chat: FakeChatWorkspace, mailbox: FakeMailbox
) -> None:
    deal = await _escalated_deal(rt)

    await send_escalation_notice(rt, deal.id, 1)

    [text] = _slack_texts(chat)
    assert "Acme Corp" in text
    assert "tier not confirmed after 3 attempts" in text
    [email] = mailbox.sent
    assert email.to == (ESCALATION_EMAIL,)
    assert email.subject == f"[NovaCRM onboarding] Escalation: Acme Corp ({deal.id})"
    assert (await _deal(rt, deal.id)).escalation_notified_at is not None
    [row] = await _notice_rows(rt)
    assert row.output_json == {"delivered": ["slack", "email"], "failed": {}}


async def test_one_working_channel_is_enough(
    rt: Runtime, chat: FakeChatWorkspace, mailbox: FakeMailbox, jobs: FakeJobScheduler
) -> None:
    deal = await _escalated_deal(rt)
    chat.fail("post_message", PermanentError("not_in_channel"))

    await send_escalation_notice(rt, deal.id, 1)

    assert len(mailbox.sent) == 1
    assert (await _deal(rt, deal.id)).escalation_notified_at is not None
    assert not jobs.jobs
    [row] = await _notice_rows(rt)
    assert row.output_json is not None
    assert row.output_json["failed"]["slack"].startswith("PermanentError")


async def test_missing_slack_channel_counts_as_a_failure(
    rt: Runtime, chat: FakeChatWorkspace, mailbox: FakeMailbox
) -> None:
    chat.channels.clear()
    deal = await _escalated_deal(rt)

    await send_escalation_notice(rt, deal.id, 1)

    [row] = await _notice_rows(rt)
    assert row.output_json is not None
    assert "not found" in row.output_json["failed"]["slack"]
    assert len(mailbox.sent) == 1


async def test_all_channels_failing_retries_then_gives_up(
    rt: Runtime, chat: FakeChatWorkspace, mailbox: FakeMailbox, jobs: FakeJobScheduler
) -> None:
    deal = await _escalated_deal(rt)
    for _ in range(3):
        chat.fail("post_message", RetryableError("slack down"))
        mailbox.fail("send", AuthorizationError("gmail revoked"))

    await send_escalation_notice(rt, deal.id, 1)
    retry = jobs.pop(f"escalation-notice:{deal.id}:2")
    assert (retry.job, retry.args) == (NOTICE_JOB, (deal.id, "2"))

    await send_escalation_notice(rt, deal.id, 2)
    jobs.pop(f"escalation-notice:{deal.id}:3")

    await send_escalation_notice(rt, deal.id, 3)
    assert not jobs.jobs  # bounded: no fourth try
    assert (await _deal(rt, deal.id)).escalation_notified_at is None
    assert len(await _notice_rows(rt)) == 3


async def test_no_channel_configured_is_recorded_without_retry(
    rt: Runtime, jobs: FakeJobScheduler
) -> None:
    bare = Runtime(
        settings=rt.settings.model_copy(update={"escalation_email": None}),
        db=rt.db,
        voice=rt.voice,
        tier_verifier=rt.tier_verifier,
        jobs=jobs,
    )
    deal = await _escalated_deal(bare)

    await send_escalation_notice(bare, deal.id, 1)

    [row] = await _notice_rows(bare)
    assert row.output_json is not None
    assert "no escalation channel configured" in row.output_json["failed"]["config"]
    assert not jobs.jobs


# --- idempotency -------------------------------------------------------------------


async def test_notice_is_sent_once_per_escalation(
    rt: Runtime, chat: FakeChatWorkspace, mailbox: FakeMailbox
) -> None:
    deal = await _escalated_deal(rt)
    await send_escalation_notice(rt, deal.id, 1)
    await send_escalation_notice(rt, deal.id, 1)  # job re-run after a crash
    assert len(_slack_texts(chat)) == 1
    assert len(mailbox.sent) == 1


async def test_resolved_deal_is_not_notified(rt: Runtime, chat: FakeChatWorkspace) -> None:
    deal = await _escalated_deal(rt)
    async with rt.db.session() as session:
        await transition(session, deal, DealStatus.AWAITING_TIER, actor="human", rationale="fixed")

    await send_escalation_notice(rt, deal.id, 1)

    assert not _slack_texts(chat)


async def test_second_escalation_gets_its_own_notice(rt: Runtime, chat: FakeChatWorkspace) -> None:
    deal = await _escalated_deal(rt, reason="first problem")
    await send_escalation_notice(rt, deal.id, 1)
    async with rt.db.session() as session:
        deal = await transition(
            session, deal, DealStatus.AWAITING_TIER, actor="human", rationale="r"
        )
        deal = await escalate(session, deal, actor="voice_agent", reason="second problem")

    await send_escalation_notice(rt, deal.id, 1)

    texts = _slack_texts(chat)
    assert len(texts) == 2
    assert "second problem" in texts[1]


def test_queue_schedules_the_notice_job(rt: Runtime, jobs: FakeJobScheduler) -> None:
    queue_escalation_notice(rt, "d_1")
    job = jobs.pop("escalation-notice:d_1:1")
    assert (job.job, job.args) == (NOTICE_JOB, ("d_1", "1"))


# --- system alerts -----------------------------------------------------------------


async def test_system_alert_goes_to_slack_only(
    rt: Runtime, chat: FakeChatWorkspace, mailbox: FakeMailbox
) -> None:
    await send_system_alert(rt, subject="Inbox down", text="Gmail token revoked")
    assert _slack_texts(chat) == ["Gmail token revoked"]
    assert not mailbox.sent


async def test_system_alert_without_slack_does_not_raise(rt: Runtime) -> None:
    bare = Runtime(
        settings=rt.settings, db=rt.db, voice=rt.voice, tier_verifier=rt.tier_verifier, jobs=rt.jobs
    )
    await send_system_alert(bare, subject="Inbox down", text="x")


# --- notice text -------------------------------------------------------------------


def test_notice_lists_attempts_and_masks_the_phone() -> None:
    deal = Deal(
        id="d_1",
        gmail_message_id="m",
        gmail_thread_id="t",
        sender_email="s@x.com",
        customer_company="Acme Corp",
        ae_name="Ravi Kumar",
        ae_phone="+919876543210",
    )
    attempts: list[Any] = [
        CallAttempt(
            deal_id="d_1",
            attempt_no=1,
            outcome="no_answer",
            outcome_reason="AE not reached (customer-busy)",
            recording_url="https://rec/1.wav",
        )
    ]
    notice = compose_notice(deal, reason="tier not confirmed", attempts=attempts)
    assert "• Call 1" in notice.text
    assert "customer-busy" in notice.text
    assert "https://rec/1.wav" in notice.text
    assert "+919876543210" not in notice.text
    assert "+91…3210" in notice.text
    assert "POST /deals/d_1/resolve" in notice.text


def test_notice_without_calls_says_so() -> None:
    deal = Deal(id="d_2", gmail_message_id="m", gmail_thread_id="t", sender_email="s@x.com")
    notice = compose_notice(deal, reason="missing fields", attempts=[])
    assert "No calls were placed" in notice.text
    assert "unknown customer" in notice.subject
