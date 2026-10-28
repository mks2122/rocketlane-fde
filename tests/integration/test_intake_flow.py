"""Intake end to end with fakes: Part 4's validation scenarios and the clarification loop."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlmodel import col, select

from app.config import AccountExecutive, AEDirectory, Settings
from app.db import Database
from app.domain import DealStatus
from app.errors import OutcomeUnknownError, PermanentError, RetryableError
from app.integrations.fakes import (
    FakeIntakeExtractor,
    FakeJobScheduler,
    FakeMailbox,
    FakeTierVerifier,
    FakeVoiceCaller,
)
from app.models import AuditLog, Deal
from app.orchestrator.intake import clarification_followup, process_intake
from app.orchestrator.poller import InboxPoller
from app.orchestrator.progress import advance
from app.orchestrator.runtime import Runtime
from app.orchestrator.workflow import handle_inbound
from app.ports import DealIntake, ExtractedField
from tests.factories import AE_EMAIL, inbound_email

SF = "https://novacrm.lightning.force.com/lightning/r/Opportunity/006Hs00001AbCdE/view"
BODY = f"""Hi team, we just closed Acme Corp!
Main contact: Jane Doe (jane.doe@acme.com).
SF: {SF}
Thanks, Ravi Kumar"""
GOOD: dict[str, tuple[str | None, str | None]] = {
    "customer_company": ("Acme Corp", "we just closed Acme Corp"),
    "contact_name": ("Jane Doe", "Main contact: Jane Doe"),
    "contact_email": ("jane.doe@acme.com", "Jane Doe (jane.doe@acme.com)"),
    "ae_name": ("Ravi Kumar", "Thanks, Ravi Kumar"),
    "ae_phone": (None, None),  # comes from the AE directory
    "sf_opportunity_url": (SF, f"SF: {SF}"),
}


def intake(handoff: bool = True, /, **overrides: tuple[str | None, str | None]) -> DealIntake:
    fields: dict[str, Any] = {
        k: ExtractedField(value=v, evidence=e) for k, (v, e) in {**GOOD, **overrides}.items()
    }
    return DealIntake(is_deal_handoff=handoff, notes=None, **fields)


@pytest.fixture
def jobs() -> FakeJobScheduler:
    return FakeJobScheduler()


@pytest.fixture
def extractor() -> FakeIntakeExtractor:
    return FakeIntakeExtractor()


@pytest.fixture
def rt(
    settings: Settings,
    db: Database,
    jobs: FakeJobScheduler,
    mailbox: FakeMailbox,
    extractor: FakeIntakeExtractor,
) -> Runtime:
    directory = AEDirectory(
        account_executives=[
            AccountExecutive(name="Ravi Kumar", email=AE_EMAIL, phone="+919876543210")
        ]
    )
    return Runtime(
        settings=settings.model_copy(update={"intake_max_clarifications": 2}),
        db=db,
        voice=FakeVoiceCaller(),
        tier_verifier=FakeTierVerifier(),
        jobs=jobs,
        mailbox=mailbox,
        intake=extractor,
        ae_directory=directory,
    )


async def _receive(rt: Runtime, mailbox: FakeMailbox, message_id: str = "m1", **kw: Any) -> str:
    email = inbound_email(message_id, thread_id="t1", body=kw.pop("body", BODY), **kw)
    mailbox.deliver(email)
    result = await handle_inbound(rt.db, rt.settings, email)
    assert result.deal_id is not None
    return result.deal_id


async def _deal(rt: Runtime, deal_id: str) -> Deal:
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
    assert deal is not None
    return deal


async def _actions(rt: Runtime, deal_id: str) -> list[str]:
    async with rt.db.session() as session:
        rows = await session.exec(
            select(AuditLog).where(AuditLog.deal_id == deal_id).order_by(col(AuditLog.id))
        )
        return [r.action for r in rows]


# --- happy path --------------------------------------------------------------------


async def test_complete_email_goes_to_the_tier_call(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor, jobs: FakeJobScheduler
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.intakes.append(intake())

    await process_intake(rt, deal_id)

    deal = await _deal(rt, deal_id)
    assert deal.status is DealStatus.AWAITING_TIER
    assert (deal.customer_company, deal.contact_email, deal.ae_phone) == (
        "Acme Corp",
        "jane.doe@acme.com",
        "+919876543210",
    )
    assert deal.sf_opportunity_url == SF
    assert f"call:{deal_id}:1" in jobs.jobs
    assert not mailbox.sent
    assert "intake" in await _actions(rt, deal_id)


async def test_intake_reads_only_the_aes_words(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.intakes.append(intake(contact_email=(None, None)))
    await process_intake(rt, deal_id)  # sends our question on the thread
    await _receive(rt, mailbox, "m2", minutes=5, body="Contact is jane.doe@acme.com")
    extractor.intakes.append(intake())

    await process_intake(rt, deal_id)

    second_read = extractor.called("extract")[1]
    assert "Contact is jane.doe@acme.com" in second_read
    assert "Before onboarding can start" not in second_read  # our own email is excluded


# --- validation (Part 4) -----------------------------------------------------------


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"contact_email": (None, None)}, "contact_email"),
        ({"contact_name": ("John Smith", "Main contact: John Smith")}, "contact_name"),  # invented
        ({"contact_email": ("jane.doe@", "Jane Doe (jane.doe@")}, "contact_email"),  # malformed
    ],
    ids=["missing", "hallucinated", "malformed"],
)
async def test_gaps_ask_the_ae_on_the_thread(
    rt: Runtime,
    mailbox: FakeMailbox,
    extractor: FakeIntakeExtractor,
    jobs: FakeJobScheduler,
    override: dict[str, tuple[str | None, str | None]],
    field: str,
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.intakes.append(intake(**override))

    await process_intake(rt, deal_id)

    deal = await _deal(rt, deal_id)
    assert deal.status is DealStatus.NEEDS_INFO
    [reply] = mailbox.sent
    assert reply.to == (AE_EMAIL,)
    assert reply.thread_id == "t1"
    assert reply.in_reply_to == "<m1@mail.example.com>"
    label = {
        "contact_email": "Customer contact's email address",
        "contact_name": "Customer contact's name",
    }
    assert label[field] in reply.body_text
    reminder = jobs.pop(f"clarify:{deal_id}:{deal.version}:reminder")
    escalation = jobs.pop(f"clarify:{deal_id}:{deal.version}:escalate")
    assert reminder.run_at - deal.updated_at == timedelta(
        seconds=rt.settings.clarification_reminder_seconds
    )
    assert escalation.run_at - deal.updated_at == timedelta(
        seconds=rt.settings.clarification_escalate_seconds
    )
    assert f"call:{deal_id}:1" not in jobs.jobs  # no call without the required data


async def test_ae_reply_completes_the_deal_and_cancels_the_timers(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor, jobs: FakeJobScheduler
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.intakes.extend([intake(contact_email=(None, None)), intake()])
    await process_intake(rt, deal_id)
    waiting = await _deal(rt, deal_id)

    await _receive(rt, mailbox, "m2", minutes=5, body="Contact is jane.doe@acme.com")
    assert (await _deal(rt, deal_id)).status is DealStatus.RECEIVED
    await process_intake(rt, deal_id)
    assert (await _deal(rt, deal_id)).status is DealStatus.AWAITING_TIER

    # The old timers fire later and find a different version: nothing happens.
    await clarification_followup(rt, deal_id, "reminder", waiting.version)
    await clarification_followup(rt, deal_id, "escalate", waiting.version)
    assert len(mailbox.sent) == 1
    assert (await _deal(rt, deal_id)).status is DealStatus.AWAITING_TIER


async def test_no_reply_gets_a_reminder_then_escalates(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor, jobs: FakeJobScheduler
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.intakes.append(intake(contact_email=(None, None)))
    await process_intake(rt, deal_id)
    version = (await _deal(rt, deal_id)).version
    jobs.jobs.clear()

    await clarification_followup(rt, deal_id, "reminder", version)
    assert mailbox.sent[-1].body_text.startswith("Hi Ravi,\n\nA quick reminder")
    assert "Customer contact's email address" in mailbox.sent[-1].body_text

    await clarification_followup(rt, deal_id, "escalate", version)
    assert (await _deal(rt, deal_id)).status is DealStatus.ESCALATED
    assert list(jobs.jobs) == [f"escalation-notice:{deal_id}:1"]
    assert "remind_ae" in await _actions(rt, deal_id)


async def test_clarification_rounds_are_capped(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor
) -> None:
    deal_id = await _receive(rt, mailbox)
    for minutes in (5, 10):
        extractor.intakes.append(intake(contact_email=(None, None)))
        await process_intake(rt, deal_id)
        await _receive(rt, mailbox, f"r{minutes}", minutes=minutes, body="still no email, sorry")
    extractor.intakes.append(intake(contact_email=(None, None)))

    await process_intake(rt, deal_id)  # third attempt: 2 rounds already used

    assert (await _deal(rt, deal_id)).status is DealStatus.ESCALATED
    assert len(mailbox.sent) == 2


async def test_not_a_handoff_is_ignored(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor
) -> None:
    deal_id = await _receive(rt, mailbox, body="Out of office until Monday.")
    extractor.intakes.append(intake(False))

    await process_intake(rt, deal_id)

    assert (await _deal(rt, deal_id)).status is DealStatus.IGNORED
    assert not mailbox.sent


# --- failures ----------------------------------------------------------------------


async def test_agent_unavailable_escalates(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor, jobs: FakeJobScheduler
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.fail("extract", PermanentError("intake_agent: HTTP 401"))

    await process_intake(rt, deal_id)

    assert (await _deal(rt, deal_id)).status is DealStatus.ESCALATED
    assert f"escalation-notice:{deal_id}:1" in jobs.jobs


async def test_unreadable_thread_retries_then_escalates(
    rt: Runtime, mailbox: FakeMailbox, jobs: FakeJobScheduler
) -> None:
    deal_id = await _receive(rt, mailbox)
    mailbox.fail("fetch_thread", *[RetryableError("gmail 503")] * 3)

    await process_intake(rt, deal_id, 1)
    assert jobs.pop(f"intake:{deal_id}:2").args == (deal_id, "2")
    await process_intake(rt, deal_id, 2)
    jobs.pop(f"intake:{deal_id}:3")
    await process_intake(rt, deal_id, 3)

    assert (await _deal(rt, deal_id)).status is DealStatus.ESCALATED


async def test_clarification_that_cannot_be_sent_escalates(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.intakes.append(intake(contact_email=(None, None)))
    mailbox.fail("send", PermanentError("gmail 403"))

    await process_intake(rt, deal_id)

    assert (await _deal(rt, deal_id)).status is DealStatus.ESCALATED


async def test_unconfirmed_send_still_waits_for_the_ae(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.intakes.append(intake(contact_email=(None, None)))
    mailbox.fail("send", OutcomeUnknownError("timeout"))

    await process_intake(rt, deal_id)

    assert (await _deal(rt, deal_id)).status is DealStatus.NEEDS_INFO


async def test_intake_running_twice_does_nothing_the_second_time(
    rt: Runtime, mailbox: FakeMailbox, extractor: FakeIntakeExtractor
) -> None:
    deal_id = await _receive(rt, mailbox)
    extractor.intakes.append(intake())
    await process_intake(rt, deal_id)
    await process_intake(rt, deal_id)  # job re-run: deal is no longer RECEIVED
    assert len(extractor.called("extract")) == 1


async def test_unconfigured_intake_escalates(rt: Runtime, mailbox: FakeMailbox) -> None:
    bare = Runtime(
        settings=rt.settings, db=rt.db, voice=rt.voice, tier_verifier=rt.tier_verifier, jobs=rt.jobs
    )
    deal_id = await _receive(bare, mailbox)
    await process_intake(bare, deal_id)
    assert (await _deal(bare, deal_id)).status is DealStatus.ESCALATED


# --- from the inbox ----------------------------------------------------------------


async def test_polled_email_queues_intake(
    rt: Runtime, mailbox: FakeMailbox, jobs: FakeJobScheduler
) -> None:
    async def next_step(deal_id: str) -> None:
        await advance(rt, await _deal(rt, deal_id))

    poller = InboxPoller(mailbox, rt.db, rt.settings, after_inbound=next_step)
    mailbox.deliver(inbound_email("m1", thread_id="t1", body=BODY))

    await poller.poll_once()

    [job_id] = list(jobs.jobs)
    assert job_id.startswith("intake:")
