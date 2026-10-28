"""handle_inbound: email → deal, idempotently, with the sender allowlist applied."""

from __future__ import annotations

import pytest
from sqlmodel import col, select

from app.config import Settings
from app.db import Database
from app.domain import DealStatus
from app.models import AuditLog, Deal
from app.orchestrator.state_machine import transition
from app.orchestrator.workflow import InboundOutcome, handle_inbound
from tests.factories import inbound_email


async def _deals(db: Database) -> list[Deal]:
    async with db.session() as session:
        return list((await session.exec(select(Deal))).all())


async def _audit(db: Database) -> list[AuditLog]:
    async with db.session() as session:
        rows = await session.exec(select(AuditLog).order_by(col(AuditLog.id)))
        return list(rows.all())


async def test_allowed_sender_creates_received_deal(db: Database, settings: Settings) -> None:
    outcome = await handle_inbound(db, settings, inbound_email("m1"))

    assert outcome.outcome is InboundOutcome.CREATED
    [deal] = await _deals(db)
    assert deal.status is DealStatus.RECEIVED
    assert deal.gmail_message_id == "m1"
    assert deal.gmail_thread_id == "t-m1"
    assert deal.sender_email == "ravi.ae@novacrm-demo.com"

    [row] = await _audit(db)
    assert (row.deal_id, row.action, row.status) == (deal.id, "receive_email", "ok")
    assert row.input_json is not None
    assert row.input_json["gmail_message_id"] == "m1"
    assert "body" not in str(row.input_json)  # the email body never reaches the audit log


async def test_same_message_twice_is_a_duplicate(db: Database, settings: Settings) -> None:
    await handle_inbound(db, settings, inbound_email("m1"))
    outcome = await handle_inbound(db, settings, inbound_email("m1"))

    assert outcome.outcome is InboundOutcome.DUPLICATE
    assert len(await _deals(db)) == 1
    assert len(await _audit(db)) == 1


async def test_unknown_sender_is_recorded_as_ignored(db: Database, settings: Settings) -> None:
    outcome = await handle_inbound(db, settings, inbound_email("m1", sender="x@spam.io"))

    assert outcome.outcome is InboundOutcome.IGNORED
    [deal] = await _deals(db)
    assert deal.status is DealStatus.IGNORED
    actions = [(r.action, r.rationale) for r in await _audit(db)]
    assert ("transition", "sender x@spam.io is not on the allowlist") in actions


async def test_empty_allowlist_fails_closed(db: Database, settings: Settings) -> None:
    closed = settings.model_copy(update={"allowed_sender_domains": [], "allowed_sender_emails": []})
    assert (await handle_inbound(db, closed, inbound_email("m1"))).outcome is InboundOutcome.IGNORED


async def test_allowed_individual_address(db: Database, settings: Settings) -> None:
    by_address = settings.model_copy(
        update={"allowed_sender_domains": [], "allowed_sender_emails": ["me@gmail.com"]}
    )
    outcome = await handle_inbound(db, by_address, inbound_email("m1", sender="Me@Gmail.com"))
    assert outcome.outcome is InboundOutcome.CREATED


async def test_reply_reopens_deal_waiting_for_info(db: Database, settings: Settings) -> None:
    await handle_inbound(db, settings, inbound_email("m1", thread_id="t1"))
    [deal] = await _deals(db)
    async with db.session() as session:
        await transition(session, deal, DealStatus.NEEDS_INFO, actor="test", rationale="setup")

    outcome = await handle_inbound(db, settings, inbound_email("m2", thread_id="t1", minutes=5))

    assert outcome.outcome is InboundOutcome.REPLY
    [deal] = await _deals(db)  # still one deal: replies never create deals
    assert deal.status is DealStatus.RECEIVED
    assert "thread_reply" in [r.action for r in await _audit(db)]


@pytest.mark.parametrize("status", [DealStatus.RECEIVED, DealStatus.IGNORED])
async def test_reply_in_other_states_is_only_recorded(
    db: Database, settings: Settings, status: DealStatus
) -> None:
    sender = "ravi.ae@novacrm-demo.com" if status is DealStatus.RECEIVED else "x@spam.io"
    await handle_inbound(db, settings, inbound_email("m1", thread_id="t1", sender=sender))

    outcome = await handle_inbound(db, settings, inbound_email("m2", thread_id="t1", minutes=5))

    assert outcome.outcome is InboundOutcome.REPLY
    [deal] = await _deals(db)
    assert deal.status is status
    assert deal.version == (1 if status is DealStatus.RECEIVED else 2)
