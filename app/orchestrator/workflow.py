"""Workflow steps: each one reads a deal, does one thing, and moves it via `transition()`.

Every step runs inside `audit_step` (decision log) and an Agents SDK `trace` grouped by
deal id, so the audit log and the trace view tell the same story.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import structlog
from agents import trace
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.config import Settings
from app.db import Database
from app.domain import DealStatus
from app.models import Deal
from app.orchestrator.state_machine import transition
from app.ports import InboundEmail
from app.services.allowlist import is_sender_allowed
from app.services.audit import audit_step, record

log = structlog.get_logger(__name__)

ACTOR_INBOX = "gmail_poller"


class InboundOutcome(StrEnum):
    CREATED = "created"  # new deal in RECEIVED, ready for intake
    IGNORED = "ignored"  # sender not allowed; deal recorded as IGNORED
    REPLY = "reply"  # message on an existing deal's thread
    DUPLICATE = "duplicate"  # this message was already handled


@dataclass(frozen=True)
class InboundResult:
    outcome: InboundOutcome
    deal_id: str | None  # None only for a duplicate lost to a concurrent insert


def _email_summary(email: InboundEmail) -> dict[str, str]:
    # Audit rows carry identifiers and the subject, never the body (PII; see standards §4).
    return {
        "gmail_message_id": email.message_id,
        "gmail_thread_id": email.thread_id,
        "from": email.from_email,
        "subject": email.subject,
        "received_at": email.received_at.isoformat(),
    }


async def handle_inbound(db: Database, settings: Settings, email: InboundEmail) -> InboundResult:
    """Record an inbound email as a deal, idempotently.

    - Same Gmail message seen again → DUPLICATE, nothing written.
    - Message on a thread that already has a deal → REPLY. If that deal is waiting
      for the AE (NEEDS_INFO), it goes back to RECEIVED so intake re-reads the thread.
    - Sender not on the allowlist → deal recorded as IGNORED (audited, never processed).
    - Otherwise → new deal in RECEIVED.
    """
    async with db.session() as session:
        known = await session.exec(
            select(Deal).where(
                (Deal.gmail_message_id == email.message_id)
                | (Deal.gmail_thread_id == email.thread_id)
            )
        )
        existing = known.first()

    if existing is not None and existing.gmail_message_id == email.message_id:
        log.debug("inbound_duplicate", deal_id=existing.id, gmail_message_id=email.message_id)
        return InboundResult(InboundOutcome.DUPLICATE, existing.id)
    if existing is not None:
        return await _handle_reply(db, existing, email)

    try:
        return await _create_deal(db, settings, email)
    except IntegrityError:
        # A concurrent handler inserted the same message id first; the DB guard held.
        log.info("inbound_duplicate_race", gmail_message_id=email.message_id)
        return InboundResult(InboundOutcome.DUPLICATE, None)


async def _create_deal(db: Database, settings: Settings, email: InboundEmail) -> InboundResult:
    deal = Deal(
        gmail_message_id=email.message_id,
        gmail_thread_id=email.thread_id,
        sender_email=email.from_email.lower(),
    )
    allowed = is_sender_allowed(
        email.from_email,
        allowed_domains=settings.allowed_sender_domains,
        allowed_emails=settings.allowed_sender_emails,
    )
    with (
        structlog.contextvars.bound_contextvars(deal_id=deal.id),
        trace("receive_email", group_id=deal.id),
    ):
        async with (
            db.session() as session,
            audit_step(
                db,
                session,
                deal_id=deal.id,
                actor=ACTOR_INBOX,
                action="receive_email",
                input=_email_summary(email),
            ) as step,
        ):
            session.add(deal)
            await session.flush()  # surfaces a duplicate message id as IntegrityError here
            if allowed:
                step.output = {"status": DealStatus.RECEIVED}
                step.rationale = "sender is on the allowlist; deal recorded for intake"
                return InboundResult(InboundOutcome.CREATED, deal.id)
            await transition(
                session,
                deal,
                DealStatus.IGNORED,
                actor=ACTOR_INBOX,
                rationale=f"sender {email.from_email} is not on the allowlist",
            )
            step.output = {"status": DealStatus.IGNORED}
            step.rationale = "sender not allowed (G6); not processed further"
            return InboundResult(InboundOutcome.IGNORED, deal.id)


async def _handle_reply(db: Database, deal: Deal, email: InboundEmail) -> InboundResult:
    with (
        structlog.contextvars.bound_contextvars(deal_id=deal.id),
        trace("thread_reply", group_id=deal.id),
    ):
        async with db.session() as session:
            reopen = deal.status is DealStatus.NEEDS_INFO
            record(
                session,
                deal_id=deal.id,
                actor=ACTOR_INBOX,
                action="thread_reply",
                input=_email_summary(email),
                rationale="AE replied while we were waiting for missing details"
                if reopen
                else f"reply on the deal thread while {deal.status}; no state change",
            )
            if reopen:
                await transition(
                    session,
                    deal,
                    DealStatus.RECEIVED,
                    actor=ACTOR_INBOX,
                    rationale="AE replied on the clarification thread; re-run intake",
                )
    return InboundResult(InboundOutcome.REPLY, deal.id)
