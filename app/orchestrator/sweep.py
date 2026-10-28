"""Recovery sweep: find deals that should be moving but have nothing queued, and queue it.

Jobs are durable, but a crash between a commit and its follow-up `schedule()`, a lost
webhook, or a job that raised can still strand a deal. The sweep runs at startup and
every SWEEP_INTERVAL_SECONDS and, for each non-terminal deal:

- waiting for a call with no call job queued → queue it (`advance`);
- escalated but the notice is still owed and not queued → queue the notice (`advance`);
- CALLING, end-of-call report received but not processed → queue the processing job;
- CALLING, no end-of-call report long after the call's max duration → close the attempt
  as a call error, so the normal retry and escalation policy applies;
- states owned by later phases are only counted.

Everything it queues is audited as `sweeper`.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from typing import Final

import structlog
from sqlmodel import col, select

from app.domain import DealStatus
from app.models import CallAttempt, Deal, utcnow
from app.orchestrator.progress import Advance, advance
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import TERMINAL
from app.orchestrator.voice import fail_attempt, queue_call_processing
from app.services.audit import record

log = structlog.get_logger(__name__)

ACTOR: Final = "sweeper"


async def sweep(rt: Runtime) -> Counter[str]:
    async with rt.db.session() as session:
        deals = (
            await session.exec(select(Deal).where(col(Deal.status).not_in(list(TERMINAL))))
        ).all()

    report: Counter[str] = Counter()
    for deal in deals:
        if deal.status is DealStatus.CALLING:
            outcome = await _recover_call(rt, deal)
        else:
            outcome = await advance(rt, deal)
        report[outcome] += 1
        if outcome not in _QUIET:
            await _audit(rt, deal, outcome)
    if sum(report[o] for o in report if o not in _QUIET):
        log.info("sweep_recovered", **report)
    return report


_QUIET: Final = frozenset(
    {Advance.ALREADY_QUEUED, Advance.NOTHING_TO_DO, Advance.NOT_AUTOMATED_YET, "call_in_progress"}
)


async def _recover_call(rt: Runtime, deal: Deal) -> str:
    async with rt.db.session() as session:
        attempt = (
            await session.exec(
                select(CallAttempt)
                .where(CallAttempt.deal_id == deal.id)
                .order_by(col(CallAttempt.attempt_no).desc())
            )
        ).first()
    if attempt is None or attempt.id is None:
        # CALLING is entered in the same transaction that creates the attempt.
        log.error("sweep_calling_without_attempt", deal_id=deal.id)
        return "inconsistent"

    if attempt.ended_at is not None and attempt.outcome is None:
        if rt.jobs.has_pending(f"process-call:{attempt.id}"):
            return Advance.ALREADY_QUEUED
        queue_call_processing(rt, attempt.id)
        return "call_processing_queued"

    deadline = attempt.started_at + timedelta(
        seconds=rt.settings.call_max_duration_seconds + rt.settings.call_report_grace_seconds
    )
    if attempt.ended_at is None and utcnow() > deadline:
        await fail_attempt(
            rt,
            attempt.id,
            f"no end-of-call report by {deadline.isoformat()}; treating the call as failed",
        )
        return "call_timed_out"
    return "call_in_progress"


async def _audit(rt: Runtime, deal: Deal, outcome: str) -> None:
    async with rt.db.session() as session:
        record(
            session,
            deal_id=deal.id,
            actor=ACTOR,
            action="recover",
            input={"status": deal.status},
            output={"queued": outcome},
            rationale="deal had no pending work for its state",
        )
