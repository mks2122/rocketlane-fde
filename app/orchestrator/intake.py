"""Intake step: turn a RECEIVED deal's email thread into validated deal fields.

    RECEIVED ─ process_intake ─┬─ complete             ─► AWAITING_TIER (+ first call queued)
                               ├─ not a deal handoff   ─► IGNORED
                               ├─ gaps, rounds left    ─► NEEDS_INFO (+ reply to the AE,
                               │                           reminder and escalation timers)
                               └─ gaps, out of rounds / mailbox or agent unavailable ─► ESCALATED
    NEEDS_INFO ─ AE replies ─► RECEIVED (handle_inbound) ─► process_intake again (whole thread)

The agent proposes (`DealIntake`), `validate_intake` decides. Clarification timers are
tied to the deal version they were set for, so any reply or human action cancels them
without anyone having to delete jobs.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final, cast

import structlog
from agents import trace
from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.agents.intake import thread_text
from app.domain import DealStatus
from app.errors import IntegrationError, OutcomeUnknownError, RetryableError
from app.models import AuditLog, Deal, utcnow
from app.orchestrator.escalation import escalate, queue_escalation_notice
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.orchestrator.voice import schedule_call_attempt
from app.ports import DealIntake, InboundEmail, OutboundEmail
from app.services.audit import audit_step, record
from app.services.clarification import compose_clarification
from app.services.intake_validation import FieldIssue, IntakeResult, Problem, validate_intake

log = structlog.get_logger(__name__)

ACTOR: Final = "intake_agent"
INTAKE_JOB: Final = "app.orchestrator.jobs:intake_job"
CLARIFICATION_JOB: Final = "app.orchestrator.jobs:clarification_job"
MAX_FETCH_ATTEMPTS: Final = 3
FETCH_RETRY_DELAY_SECONDS: Final = 300
REQUEST_INFO: Final = "request_info"


def queue_intake(rt: Runtime, deal_id: str, *, attempt: int = 1, delay_s: int = 0) -> None:
    rt.jobs.schedule(
        INTAKE_JOB,
        run_at=utcnow() + timedelta(seconds=delay_s),
        args=[deal_id, str(attempt)],
        job_id=f"intake:{deal_id}:{attempt}",
    )


def queue_clarification_followups(rt: Runtime, deal: Deal) -> None:
    """Reminder and escalation timers for this NEEDS_INFO episode (keyed by version)."""
    started = deal.updated_at
    for stage, after in (
        ("reminder", rt.settings.clarification_reminder_seconds),
        ("escalate", rt.settings.clarification_escalate_seconds),
    ):
        rt.jobs.schedule(
            CLARIFICATION_JOB,
            run_at=started + timedelta(seconds=after),
            args=[deal.id, stage, str(deal.version)],
            job_id=f"clarify:{deal.id}:{deal.version}:{stage}",
        )


async def process_intake(rt: Runtime, deal_id: str, attempt: int = 1) -> None:
    """Read, validate and route a RECEIVED deal. Safe to run twice."""
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
    if deal is None or deal.status is not DealStatus.RECEIVED:
        log.info("intake_skipped", deal_id=deal_id, status=deal.status if deal else None)
        return

    with (
        structlog.contextvars.bound_contextvars(deal_id=deal_id),
        trace("intake", group_id=deal_id),
    ):
        if rt.mailbox is None or rt.intake is None or rt.ae_directory is None:
            await _escalate(rt, deal, "intake is not configured (Gmail, OpenAI or AE directory)")
            return

        try:
            thread = await rt.mailbox.fetch_thread(deal.gmail_thread_id)
        except RetryableError as exc:
            if attempt < MAX_FETCH_ATTEMPTS:
                queue_intake(rt, deal_id, attempt=attempt + 1, delay_s=FETCH_RETRY_DELAY_SECONDS)
                log.warning("intake_fetch_retry_scheduled", attempt=attempt, error=str(exc))
                return
            await _escalate(rt, deal, f"could not read the email thread: {exc}")
            return
        except IntegrationError as exc:
            await _escalate(rt, deal, f"could not read the email thread: {exc}")
            return

        ae_messages = [e for e in thread if not e.from_self]
        if not ae_messages:
            await _escalate(rt, deal, "no readable message from the AE on the thread")
            return
        text = thread_text(thread)

        try:
            intake = await rt.intake.extract(thread_text=text)
        except IntegrationError as exc:
            await _escalate(
                rt, deal, f"intake agent unavailable ({type(exc).__name__}); read by hand"
            )
            return

        sender = ae_messages[0]
        result = validate_intake(
            intake,
            thread_text=text,
            sender_email=deal.sender_email,
            sender_name=sender.from_name,
            directory_entry=rt.ae_directory.lookup(deal.sender_email),
            required=rt.settings.intake_required_fields,
        )
        await _route(rt, deal, intake, result, ae_messages)


async def _route(
    rt: Runtime,
    deal: Deal,
    intake: DealIntake,
    result: IntakeResult,
    ae_messages: list[InboundEmail],
) -> None:
    summary: dict[str, Any] = {
        "is_deal_handoff": intake.is_deal_handoff,
        "values": result.values,
        "sources": result.sources,
        "issues": [_issue_json(i) for i in result.issues],
    }
    audit_input = {"messages": [e.message_id for e in ae_messages]}

    if not intake.is_deal_handoff:
        async with (
            rt.db.session() as session,
            audit_step(
                rt.db, session, deal_id=deal.id, actor=ACTOR, action="intake", input=audit_input
            ) as step,
        ):
            await transition(
                session,
                deal,
                DealStatus.IGNORED,
                actor=ACTOR,
                rationale="intake agent: the email is not a deal handoff",
            )
            step.output, step.rationale = summary, "not a deal handoff"
        return

    if result.complete:
        async with (
            rt.db.session() as session,
            audit_step(
                rt.db, session, deal_id=deal.id, actor=ACTOR, action="intake", input=audit_input
            ) as step,
        ):
            updated = await transition(
                session,
                deal,
                DealStatus.AWAITING_TIER,
                actor=ACTOR,
                rationale="all required fields present, grounded and valid",
                changes=result.values,
            )
            step.output, step.rationale = summary, "complete; confirming the tier by phone"
        schedule_call_attempt(rt, updated.id, earliest=utcnow(), attempt_no=1)
        return

    rounds = await _clarification_rounds(rt, deal.id)
    missing = ", ".join(i.field for i in result.issues)
    if rounds >= rt.settings.intake_max_clarifications:
        await _escalate(
            rt, deal, f"still missing after {rounds} clarification round(s): {missing}", summary
        )
        return
    await _ask_the_ae(rt, deal, result, ae_messages, summary, audit_input)


async def _ask_the_ae(
    rt: Runtime,
    deal: Deal,
    result: IntakeResult,
    ae_messages: list[InboundEmail],
    summary: dict[str, Any],
    audit_input: dict[str, Any],
) -> None:
    assert rt.mailbox is not None  # noqa: S101  # checked by process_intake
    latest = ae_messages[-1]
    message = compose_clarification(
        ae_name=result.values.get("ae_name"),
        customer_company=result.values.get("customer_company"),
        original_subject=ae_messages[0].subject,
        issues=result.issues,
    )
    # Sent before the state change: if we can't ask, a human must be told instead of the
    # deal waiting on a reply that can never come.
    try:
        await rt.mailbox.send(
            OutboundEmail(
                to=(deal.sender_email,),
                subject=message.subject,
                body_text=message.body,
                thread_id=deal.gmail_thread_id,
                in_reply_to=latest.rfc822_message_id,
            )
        )
    except OutcomeUnknownError:
        log.warning("clarification_send_unconfirmed")  # most likely sent; don't risk a duplicate
    except IntegrationError as exc:
        await _escalate(rt, deal, f"could not email the AE for missing details: {exc}", summary)
        return

    async with (
        rt.db.session() as session,
        audit_step(
            rt.db, session, deal_id=deal.id, actor=ACTOR, action=REQUEST_INFO, input=audit_input
        ) as step,
    ):
        known = {k: v for k, v in result.values.items() if k != "ae_email"}
        updated = await transition(
            session,
            deal,
            DealStatus.NEEDS_INFO,
            actor=ACTOR,
            rationale=f"asked the AE for: {', '.join(i.field for i in result.issues)}",
            changes=known,
        )
        step.output, step.rationale = summary, "missing or invalid fields; asked the AE"
    queue_clarification_followups(rt, updated)


async def clarification_followup(rt: Runtime, deal_id: str, stage: str, version: int) -> None:
    """Reminder (+24h) or escalation (+48h) for an unanswered clarification."""
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
        issues = await _latest_issues(session, deal_id)
    if deal is None or deal.status is not DealStatus.NEEDS_INFO or deal.version != version:
        log.info("clarification_followup_skipped", deal_id=deal_id, stage=stage)
        return  # the AE replied, or a human acted: this timer belongs to an old episode

    with structlog.contextvars.bound_contextvars(deal_id=deal_id):
        if stage == "escalate":
            hours = rt.settings.clarification_escalate_seconds // 3600
            missing = ", ".join(i.field for i in issues) or "see the audit log"
            await _escalate(
                rt, deal, f"the AE did not reply within {hours}h; still missing: {missing}"
            )
            return
        if rt.mailbox is None:
            return
        message = compose_clarification(
            ae_name=deal.ae_name,
            customer_company=deal.customer_company,
            original_subject="",
            issues=issues,
            reminder=True,
        )
        try:
            await rt.mailbox.send(
                OutboundEmail(
                    to=(deal.sender_email,),
                    subject=message.subject,
                    body_text=message.body,
                    thread_id=deal.gmail_thread_id,
                )
            )
        except IntegrationError as exc:
            # The escalation timer still fires: a failed reminder never strands the deal.
            log.warning("clarification_reminder_failed", error=str(exc))
            return
        async with rt.db.session() as session:
            record(
                session, deal_id=deal_id, actor=ACTOR, action="remind_ae", rationale="no reply yet"
            )


async def _clarification_rounds(rt: Runtime, deal_id: str) -> int:
    async with rt.db.session() as session:
        result = await session.exec(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.deal_id == deal_id,
                AuditLog.action == REQUEST_INFO,
                AuditLog.status == "ok",
            )
        )
        return result.one()


async def _latest_issues(session: AsyncSession, deal_id: str) -> list[FieldIssue]:
    row = (
        await session.exec(
            select(AuditLog)
            .where(AuditLog.deal_id == deal_id, AuditLog.action == REQUEST_INFO)
            .order_by(col(AuditLog.id).desc())
        )
    ).first()
    if row is None:
        return []
    raw = cast(list[dict[str, str]], (row.output_json or {}).get("issues") or [])
    return [FieldIssue(i["field"], Problem(i["problem"]), i["detail"]) for i in raw]


def _issue_json(issue: FieldIssue) -> dict[str, str]:
    return {"field": issue.field, "problem": issue.problem.value, "detail": issue.detail}


async def _escalate(
    rt: Runtime, deal: Deal, reason: str, output: dict[str, Any] | None = None
) -> None:
    async with rt.db.session() as session:
        if output is not None:
            record(
                session,
                deal_id=deal.id,
                actor=ACTOR,
                action="intake",
                output=output,
                rationale=reason,
            )
        await escalate(session, deal, actor=ACTOR, reason=reason)
    queue_escalation_notice(rt, deal.id)
