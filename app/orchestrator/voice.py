"""Voice step: phone the AE, collect the tier, and decide what the call established.

Flow:
    AWAITING_TIER / CALL_RETRY_WAIT
      └─ start_call_attempt ─► CALLING ─ (Vapi webhooks) ─► record_tool_calls
                                       └─ end-of-call-report ─► process_call_result
                                              ├─ CONFIRMED            ─► TIER_CONFIRMED
                                              ├─ attempts left        ─► CALL_RETRY_WAIT (+ job)
                                              └─ out of attempts / verifier down ─► ESCALATED

The webhook handlers only store what Vapi sent and enqueue a durable job. Deciding
(which may call the LLM) happens in the job, so a slow model never times out a webhook
and a crash mid-decision is retried on restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

import structlog
from agents import trace
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.domain import CallOutcome, DealStatus, TierAnswer, TierSource
from app.errors import IntegrationError, ValidationFailedError
from app.integrations.vapi import (
    MAX_TRANSCRIPT_CHARS,
    TOOL_RECORD_TIER,
    RecordPlanTierArgs,
    VapiCall,
    VapiMessage,
    parse_tool_args,
    tool_result,
)
from app.models import CallAttempt, Deal, WebhookEvent, utcnow
from app.orchestrator.escalation import escalate, queue_escalation_notice
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.ports import OutboundCall, TierVerdict
from app.services.audit import audit_step, record
from app.services.call_policy import (
    CallDecision,
    ToolReport,
    classify_without_verifier,
    next_call_time,
    two_key_decision,
)
from app.services.escalation_notice import mask_phone

log = structlog.get_logger(__name__)

ACTOR: Final = "voice_agent"
START_CALL_JOB: Final = "app.orchestrator.jobs:start_call_job"
PROCESS_CALL_JOB: Final = "app.orchestrator.jobs:process_call_job"
# A job that fires a little early (clock skew) shouldn't be pushed to the next window.
WINDOW_TOLERANCE: Final = timedelta(seconds=60)
CALLABLE_STATES: Final = frozenset({DealStatus.AWAITING_TIER, DealStatus.CALL_RETRY_WAIT})

REPLY_CONFIRMED: Final = (
    "Recorded. Thank the AE, tell them onboarding will start shortly, and end the call."
)
REPLY_UNCLEAR: Final = (
    "Recorded as unclear. Thank the AE, say the onboarding team will follow up, and end the call."
)


def _call_window(rt: Runtime, earliest: datetime) -> datetime:
    s = rt.settings
    return next_call_time(
        earliest,
        window_start=s.call_window_start,
        window_end=s.call_window_end,
        days=s.call_days,
        timezone=s.call_timezone,
    )


# --- scheduling -------------------------------------------------------------------


def schedule_call_attempt(
    rt: Runtime, deal_id: str, *, earliest: datetime, attempt_no: int
) -> datetime:
    """Queue the next call for the first moment inside the AE's calling window."""
    run_at = _call_window(rt, earliest)
    rt.jobs.schedule(
        START_CALL_JOB, run_at=run_at, args=[deal_id], job_id=f"call:{deal_id}:{attempt_no}"
    )
    return run_at


# --- starting a call --------------------------------------------------------------


async def start_call_attempt(rt: Runtime, deal_id: str) -> None:
    """Place the next call for a deal waiting on its tier. Safe to run twice."""
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
        attempts_made = await _attempt_count(session, deal_id)
    if deal is None or deal.status not in CALLABLE_STATES:
        log.info("call_skipped", deal_id=deal_id, status=deal.status if deal else None)
        return
    attempt_no = attempts_made + 1

    now = utcnow()
    allowed_at = _call_window(rt, now)
    if allowed_at - now > WINDOW_TOLERANCE:
        schedule_call_attempt(rt, deal_id, earliest=allowed_at, attempt_no=attempt_no)
        log.info("call_deferred_to_window", deal_id=deal_id, run_at=allowed_at.isoformat())
        return

    with (
        structlog.contextvars.bound_contextvars(deal_id=deal_id),
        trace("voice_call", group_id=deal_id),
    ):
        missing = [f for f in ("ae_phone", "ae_name", "customer_company") if not getattr(deal, f)]
        if missing:
            await _escalate(rt, deal, f"cannot call: deal is missing {', '.join(missing)}")
            return
        assert deal.ae_phone  # noqa: S101  # checked above
        assert deal.ae_name  # noqa: S101
        assert deal.customer_company  # noqa: S101

        async with (
            rt.db.session() as session,
            audit_step(
                rt.db,
                session,
                deal_id=deal_id,
                actor=ACTOR,
                action="start_call",
                input={"attempt_no": attempt_no, "to": mask_phone(deal.ae_phone)},
            ) as step,
        ):
            await transition(
                session,
                deal,
                DealStatus.CALLING,
                actor=ACTOR,
                rationale=f"call attempt {attempt_no} of {rt.settings.call_max_attempts}",
            )
            attempt = CallAttempt(deal_id=deal_id, attempt_no=attempt_no)
            session.add(attempt)
            await session.flush()
            attempt_id = attempt.id
            step.output = {"call_attempt_id": attempt_id}
            step.rationale = "tier not yet confirmed; calling the AE"
        # Committed before dialling: the webhook needs the attempt row, and a crash after
        # dialling must not lose the fact that an attempt was made.
        assert attempt_id is not None  # noqa: S101  # assigned by flush

        call = OutboundCall(
            deal_id=deal_id,
            attempt_no=attempt_no,
            to_number=deal.ae_phone,
            ae_name=deal.ae_name,
            customer_company=deal.customer_company,
        )
        try:
            call_id = await rt.voice.start_call(call)
        except IntegrationError as exc:
            # Includes OutcomeUnknownError: we don't redial at once (that could ring the AE
            # twice); the normal retry schedule applies.
            reason = f"could not start call: {type(exc).__name__}: {exc}"
            await _finish_attempt(
                rt, attempt_id, CallDecision(CallOutcome.CALL_ERROR, None, reason)
            )
            return

        async with rt.db.session() as session:
            stored = await session.get(CallAttempt, attempt_id)
            assert stored is not None  # noqa: S101  # committed above
            stored.vapi_call_id = call_id
            record(
                session,
                deal_id=deal_id,
                actor=ACTOR,
                action="call_started",
                output={"vapi_call_id": call_id, "attempt_no": attempt_no},
            )


async def _attempt_count(session: AsyncSession, deal_id: str) -> int:
    result = await session.exec(
        select(func.count()).select_from(CallAttempt).where(CallAttempt.deal_id == deal_id)
    )
    return result.one()


# --- webhooks ---------------------------------------------------------------------


async def _find_attempt(session: AsyncSession, call: VapiCall | None) -> CallAttempt | None:
    if call is None:
        return None
    found = await session.exec(select(CallAttempt).where(CallAttempt.vapi_call_id == call.id))
    attempt = found.first()
    if attempt is None and (ref := call.deal_ref()) is not None:
        # The webhook beat the POST /call response: link the call id now.
        deal_id, attempt_no = ref
        by_ref = await session.exec(
            select(CallAttempt).where(
                CallAttempt.deal_id == deal_id, CallAttempt.attempt_no == attempt_no
            )
        )
        attempt = by_ref.first()
        if attempt is not None and attempt.vapi_call_id is None:
            attempt.vapi_call_id = call.id
    return attempt


async def record_tool_calls(rt: Runtime, message: VapiMessage) -> dict[str, list[dict[str, str]]]:
    """Store `record_plan_tier` results; reply to the assistant with what to do next."""
    results: list[dict[str, str]] = []
    async with rt.db.session() as session:
        attempt = await _find_attempt(session, message.call)
        for tool_call in message.toolCallList:
            if tool_call.function.name != TOOL_RECORD_TIER:
                results.append(
                    tool_result(tool_call, f"error: unknown tool {tool_call.function.name}")
                )
                continue
            try:
                args = parse_tool_args(tool_call.function.arguments)
            except ValidationFailedError as exc:
                # Tell the assistant, so it can ask again and call the tool correctly.
                results.append(tool_result(tool_call, f"error: {exc}. Call the tool again."))
                continue
            if attempt is None or attempt.outcome is not None:
                log.warning("tool_call_unmatched", vapi_call_id=message.call and message.call.id)
                results.append(tool_result(tool_call, REPLY_UNCLEAR))
                continue
            _store_tool_report(session, attempt, args)
            confirmed = args.ae_confirmed and args.tier is not TierAnswer.UNCLEAR
            results.append(tool_result(tool_call, REPLY_CONFIRMED if confirmed else REPLY_UNCLEAR))
    return {"results": results}


def _store_tool_report(
    session: AsyncSession, attempt: CallAttempt, args: RecordPlanTierArgs
) -> None:
    # The latest call wins: the assistant may record "unclear", re-ask, then record a tier.
    attempt.tool_tier = args.tier.value
    attempt.tool_confirmed = args.ae_confirmed
    attempt.tool_quote = args.ae_quote
    record(
        session,
        deal_id=attempt.deal_id,
        actor=ACTOR,
        action=TOOL_RECORD_TIER,
        input={"attempt_no": attempt.attempt_no},
        output=args,
        rationale="voice assistant recorded the AE's answer (key 1 of 2)",
    )


async def record_end_of_call(rt: Runtime, message: VapiMessage) -> None:
    """Store the call's outcome and queue the decision. Deduplicated per call."""
    if message.call is None:
        log.warning("end_of_call_without_call")
        return
    call_id = message.call.id
    try:
        async with rt.db.session() as session:
            session.add(WebhookEvent(call_id=call_id, message_type=message.type))
            await session.flush()  # a redelivery fails here, before anything else changes
            attempt = await _find_attempt(session, message.call)
            if attempt is None:
                log.error("end_of_call_unmatched", vapi_call_id=call_id)
                return
            artifact = message.artifact
            transcript = (artifact.transcript if artifact else None) or ""
            attempt.ended_reason = message.endedReason
            attempt.transcript = transcript[:MAX_TRANSCRIPT_CHARS]
            attempt.recording_url = artifact.recording_link() if artifact else None
            attempt.ended_at = utcnow()
            attempt_id = attempt.id
    except IntegrityError:
        log.info("end_of_call_duplicate", vapi_call_id=call_id)
        return
    assert attempt_id is not None  # noqa: S101
    rt.jobs.schedule(
        PROCESS_CALL_JOB,
        run_at=utcnow(),
        args=[str(attempt_id)],
        job_id=f"process-call:{attempt_id}",
    )


# --- deciding ---------------------------------------------------------------------


async def process_call_result(rt: Runtime, attempt_id: int) -> None:
    """Turn a finished call into a decision. Safe to run twice."""
    async with rt.db.session() as session:
        attempt = await session.get(CallAttempt, attempt_id)
        deal = await session.get(Deal, attempt.deal_id) if attempt else None
    if attempt is None or deal is None or attempt.outcome is not None:
        log.info("call_result_already_processed", call_attempt_id=attempt_id)
        return

    with (
        structlog.contextvars.bound_contextvars(deal_id=deal.id),
        trace("voice_call_decision", group_id=deal.id),
    ):
        tool = (
            ToolReport(
                tier=TierAnswer(attempt.tool_tier),
                ae_confirmed=bool(attempt.tool_confirmed),
                ae_quote=attempt.tool_quote or "",
            )
            if attempt.tool_tier
            else None
        )
        decision = classify_without_verifier(attempt.ended_reason, tool)
        verdict: TierVerdict | None = None
        if decision is None:
            assert tool is not None  # noqa: S101  # only None when a confirmed tool report exists
            try:
                verdict = await rt.tier_verifier.verify(
                    transcript=attempt.transcript or "",
                    customer_company=deal.customer_company or "",
                )
            except IntegrationError as exc:
                # The AE did answer; calling again won't help. A human checks the recording.
                reason = f"tier verifier unavailable ({type(exc).__name__}); needs human review"
                decision = CallDecision(CallOutcome.UNCLEAR, None, reason)
                await _finish_attempt(rt, attempt_id, decision, verdict=None, escalate_now=True)
                return
            decision = two_key_decision(tool, verdict, attempt.transcript or "")
        await _finish_attempt(rt, attempt_id, decision, verdict=verdict)


async def _finish_attempt(
    rt: Runtime,
    attempt_id: int,
    decision: CallDecision,
    *,
    verdict: TierVerdict | None = None,
    escalate_now: bool = False,
) -> None:
    max_attempts = rt.settings.call_max_attempts
    retry_at: datetime | None = None
    escalated = False
    async with rt.db.session() as session:
        attempt = await session.get(CallAttempt, attempt_id)
        assert attempt is not None  # noqa: S101
        deal = await session.get(Deal, attempt.deal_id)
        assert deal is not None  # noqa: S101
        if deal.status is not DealStatus.CALLING or attempt.outcome is not None:
            log.info("call_decision_skipped", status=deal.status, outcome=attempt.outcome)
            return

        async with audit_step(
            rt.db,
            session,
            deal_id=deal.id,
            actor=ACTOR,
            action="decide_tier",
            input={
                "attempt_no": attempt.attempt_no,
                "ended_reason": attempt.ended_reason,
                "tool": {"tier": attempt.tool_tier, "confirmed": attempt.tool_confirmed},
                "verifier": verdict,
            },
        ) as step:
            attempt.outcome = decision.outcome.value
            attempt.outcome_reason = decision.reason
            attempt.ended_at = attempt.ended_at or utcnow()
            if verdict is not None:
                attempt.verifier_tier = verdict.tier.value
                attempt.verifier_confirmed = verdict.ae_confirmed
                attempt.verifier_quote = verdict.evidence_quote

            if decision.outcome is CallOutcome.CONFIRMED:
                await transition(
                    session,
                    deal,
                    DealStatus.TIER_CONFIRMED,
                    actor=ACTOR,
                    rationale=decision.reason,
                    changes={"plan_tier": decision.tier, "tier_source": TierSource.VOICE},
                )
            elif escalate_now or attempt.attempt_no >= max_attempts:
                why = decision.reason
                if not escalate_now:
                    why = f"tier not confirmed after {attempt.attempt_no} attempts; last: {why}"
                await escalate(session, deal, actor=ACTOR, reason=why)
                escalated = True
            else:
                delay = rt.settings.call_retry_delays_seconds[attempt.attempt_no - 1]
                retry_at = _call_window(rt, utcnow() + timedelta(seconds=delay))
                await transition(
                    session,
                    deal,
                    DealStatus.CALL_RETRY_WAIT,
                    actor=ACTOR,
                    rationale=f"{decision.reason}; next attempt at {retry_at.isoformat()}",
                )
            step.output = {"outcome": decision.outcome, "tier": decision.tier, "retry_at": retry_at}
            step.rationale = decision.reason
            next_attempt = attempt.attempt_no + 1
            deal_id = deal.id

    # After commit, so a job never acts on state that might still roll back.
    if retry_at is not None:
        schedule_call_attempt(rt, deal_id, earliest=retry_at, attempt_no=next_attempt)
    if escalated:
        queue_escalation_notice(rt, deal_id)


async def _escalate(rt: Runtime, deal: Deal, reason: str) -> None:
    async with rt.db.session() as session:
        await escalate(session, deal, actor=ACTOR, reason=reason)
    queue_escalation_notice(rt, deal.id)
