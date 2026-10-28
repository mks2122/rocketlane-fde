"""Move a deal forward from whatever state it is in. Used by human resolution,
`POST /deals/{id}/retry` and the recovery sweep.

`advance()` only queues work. It never performs a step inline, so it is cheap, safe to
call repeatedly, and leaves timing (calling windows, retries) to the steps themselves.
Statuses whose next step belongs to a later phase (Rocketlane and Slack: P7/P8) return
a "not automated yet" note instead of doing anything.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

import structlog
from sqlmodel import func, select

from app.domain import DealStatus
from app.models import CallAttempt, Deal, utcnow
from app.orchestrator.escalation import queue_escalation_notice
from app.orchestrator.intake import queue_clarification_followups, queue_intake
from app.orchestrator.runtime import Runtime
from app.orchestrator.voice import schedule_call_attempt

log = structlog.get_logger(__name__)

CALL_STATES: Final = frozenset({DealStatus.AWAITING_TIER, DealStatus.CALL_RETRY_WAIT})


class Advance(StrEnum):
    INTAKE_QUEUED = "intake_queued"
    FOLLOWUPS_QUEUED = "followups_queued"
    CALL_QUEUED = "call_queued"
    NOTICE_QUEUED = "notice_queued"
    ALREADY_QUEUED = "already_queued"
    NOTHING_TO_DO = "nothing_to_do"
    NOT_AUTOMATED_YET = "not_automated_yet"


async def advance(rt: Runtime, deal: Deal) -> Advance:
    if deal.status is DealStatus.RECEIVED:
        if rt.jobs.has_pending(f"intake:{deal.id}:"):
            return Advance.ALREADY_QUEUED
        queue_intake(rt, deal.id)
        return Advance.INTAKE_QUEUED

    if deal.status is DealStatus.NEEDS_INFO:
        # Waiting on the AE; make sure this episode's reminder/escalation timers exist.
        if rt.jobs.has_pending(f"clarify:{deal.id}:{deal.version}:"):
            return Advance.ALREADY_QUEUED
        queue_clarification_followups(rt, deal)
        return Advance.FOLLOWUPS_QUEUED

    if deal.status in CALL_STATES:
        if rt.jobs.has_pending(f"call:{deal.id}:"):
            return Advance.ALREADY_QUEUED
        async with rt.db.session() as session:
            made = (
                await session.exec(
                    select(func.count())
                    .select_from(CallAttempt)
                    .where(CallAttempt.deal_id == deal.id)
                )
            ).one()
        schedule_call_attempt(rt, deal.id, earliest=utcnow(), attempt_no=made + 1)
        return Advance.CALL_QUEUED

    if deal.status is DealStatus.ESCALATED:
        owed = deal.escalated_at is not None and (
            deal.escalation_notified_at is None or deal.escalation_notified_at < deal.escalated_at
        )
        if not owed:
            return Advance.NOTHING_TO_DO  # waiting for a human, who has been told
        if rt.jobs.has_pending(f"escalation-notice:{deal.id}:"):
            return Advance.ALREADY_QUEUED
        queue_escalation_notice(rt, deal.id)
        return Advance.NOTICE_QUEUED

    if deal.status in {DealStatus.IGNORED, DealStatus.COMPLETED, DealStatus.CALLING}:
        # Terminal, or a call is live (the sweep handles calls that never report back).
        return Advance.NOTHING_TO_DO

    log.info("advance_not_automated_yet", deal_id=deal.id, status=deal.status)
    return Advance.NOT_AUTOMATED_YET
