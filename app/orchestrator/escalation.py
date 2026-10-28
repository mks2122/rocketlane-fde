"""Escalation: hand a deal to a human, and make sure a human actually hears about it.

`escalate()` moves the deal to ESCALATED inside the caller's transaction. The caller then
calls `queue_escalation_notice()` after commit, so the notice job never runs before the
state it reports exists. The job (`send_escalation_notice`) posts to the Slack
escalation channel and emails the escalation address:

- each channel is tried independently, and one success is enough;
- if every channel fails, the job reschedules itself after
  `ESCALATION_NOTICE_RETRY_DELAYS_SECONDS`, then gives up loudly (ERROR log + audit row);
- it is idempotent: a deal whose current escalation was already notified, or that a
  human already resolved, is skipped.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

import structlog
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.domain import DealStatus
from app.errors import IntegrationError
from app.models import AuditLog, CallAttempt, Deal, utcnow
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.ports import OutboundEmail
from app.services.audit import record
from app.services.escalation_notice import EscalationNotice, compose_notice

log = structlog.get_logger(__name__)

ACTOR: Final = "escalation"
NOTICE_JOB: Final = "app.orchestrator.jobs:escalation_notice_job"


async def escalate(session: AsyncSession, deal: Deal, *, actor: str, reason: str) -> Deal:
    """Move the deal to ESCALATED in the caller's transaction. Queue the notice after commit."""
    escalated = await transition(session, deal, DealStatus.ESCALATED, actor=actor, rationale=reason)
    log.warning("deal_escalated", deal_id=deal.id, reason=reason)
    return escalated


def queue_escalation_notice(
    rt: Runtime, deal_id: str, *, attempt: int = 1, delay_s: int = 0
) -> None:
    rt.jobs.schedule(
        NOTICE_JOB,
        run_at=utcnow() + timedelta(seconds=delay_s),
        args=[deal_id, str(attempt)],
        job_id=f"escalation-notice:{deal_id}:{attempt}",
    )


async def send_escalation_notice(rt: Runtime, deal_id: str, attempt: int) -> None:
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
        if deal is None or not _notice_owed(deal):
            log.info("escalation_notice_skipped", deal_id=deal_id)
            return
        reason = await _escalation_reason(session, deal_id)
        attempts = (
            await session.exec(
                select(CallAttempt)
                .where(CallAttempt.deal_id == deal_id)
                .order_by(col(CallAttempt.attempt_no))
            )
        ).all()
        notice = compose_notice(deal, reason=reason, attempts=attempts)

    delivered, failures = await _deliver(rt, notice)

    async with rt.db.session() as session:
        fresh = await session.get(Deal, deal_id)
        assert fresh is not None  # noqa: S101  # deals are never deleted
        if delivered:
            fresh.escalation_notified_at = utcnow()
        record(
            session,
            deal_id=deal_id,
            actor=ACTOR,
            action="escalation_notice",
            input={"attempt": attempt},
            output={"delivered": delivered, "failed": failures},
            rationale="notified a human" if delivered else "no channel accepted the notice",
        )

    if delivered:
        log.info("escalation_notice_sent", deal_id=deal_id, channels=delivered)
        return
    delays = rt.settings.escalation_notice_retry_delays_seconds
    if attempt <= len(delays) and (rt.chat or rt.mailbox):
        queue_escalation_notice(rt, deal_id, attempt=attempt + 1, delay_s=delays[attempt - 1])
        log.warning("escalation_notice_retry_scheduled", deal_id=deal_id, failures=failures)
    else:
        # Nothing left to try automatically. ERROR logs are the last line of defence.
        log.error("escalation_notice_gave_up", deal_id=deal_id, failures=failures)


async def send_system_alert(rt: Runtime, *, subject: str, text: str) -> None:
    """Best-effort alert that isn't about one deal (e.g. the inbox lost its Gmail access).

    Slack only: the usual failure here is Gmail itself, so email can't be relied on.
    """
    if rt.chat is None:
        log.error("system_alert_undeliverable", subject=subject, reason="Slack not configured")
        return
    delivered, failures = await _deliver(
        rt, EscalationNotice(subject=subject, text=text), email=False
    )
    if not delivered:
        log.error("system_alert_failed", subject=subject, failures=failures)


def _notice_owed(deal: Deal) -> bool:
    if deal.status is not DealStatus.ESCALATED or deal.escalated_at is None:
        return False  # resolved before the job ran
    return deal.escalation_notified_at is None or deal.escalation_notified_at < deal.escalated_at


async def _escalation_reason(session: AsyncSession, deal_id: str) -> str:
    rows = await session.exec(
        select(AuditLog)
        .where(AuditLog.deal_id == deal_id, AuditLog.action == "transition")
        .order_by(col(AuditLog.id).desc())
    )
    for row in rows:
        if (row.input_json or {}).get("to") == DealStatus.ESCALATED.value:
            return row.rationale or "no reason recorded"
    return "no reason recorded"


async def _deliver(
    rt: Runtime, notice: EscalationNotice, *, email: bool = True
) -> tuple[list[str], dict[str, str]]:
    delivered: list[str] = []
    failures: dict[str, str] = {}
    settings = rt.settings

    if rt.chat is not None:
        try:
            channel = await rt.chat.find_channel(settings.slack_escalation_channel)
            if channel is None:
                failures["slack"] = f"channel #{settings.slack_escalation_channel} not found"
            else:
                await rt.chat.post_message(channel.channel_id, notice.text)
                delivered.append("slack")
        except IntegrationError as exc:
            failures["slack"] = f"{type(exc).__name__}: {exc}"

    if email and rt.mailbox is not None and settings.escalation_email is not None:
        try:
            await rt.mailbox.send(
                OutboundEmail(
                    to=(settings.escalation_email,), subject=notice.subject, body_text=notice.text
                )
            )
            delivered.append("email")
        except IntegrationError as exc:
            failures["email"] = f"{type(exc).__name__}: {exc}"

    if not delivered and not failures:
        failures["config"] = "no escalation channel configured (Slack or ESCALATION_EMAIL)"
    return delivered, failures
