"""The voice step end to end, with fakes for Vapi, the verifier and the scheduler.

Covers the Part 4 voice scenarios: happy path, no answer → retries → escalation, ambiguous
tier, keys disagreeing, verifier down, plus idempotency and the calling window.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import col, select

from app.config import Settings
from app.db import Database
from app.domain import CallOutcome, DealStatus, PlanTier, TierAnswer, TierSource
from app.errors import OutcomeUnknownError, PermanentError
from app.integrations.fakes import FakeJobScheduler, FakeTierVerifier, FakeVoiceCaller
from app.integrations.vapi import VapiMessage, VapiWebhook
from app.models import AuditLog, CallAttempt, Deal
from app.orchestrator import voice as flow
from app.orchestrator.resolution import Resolution, ResolveAction, resolve
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.ports import TierVerdict

IST = ZoneInfo("Asia/Kolkata")
WED_10AM = datetime(2026, 10, 28, 10, 0, tzinfo=IST).astimezone(UTC)
TRANSCRIPT = (
    "AI: Which plan did Acme Corp sign up for: Enterprise or Growth?\n"
    "User: Enterprise.\nAI: Just to confirm, Enterprise, correct?\nUser: Yes, correct."
)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(WED_10AM)
    monkeypatch.setattr(flow, "utcnow", clock)
    return clock


@pytest.fixture
def verifier() -> FakeTierVerifier:
    return FakeTierVerifier()


@pytest.fixture
def jobs() -> FakeJobScheduler:
    return FakeJobScheduler()


@pytest.fixture
def rt(
    settings: Settings,
    db: Database,
    voice: FakeVoiceCaller,
    verifier: FakeTierVerifier,
    jobs: FakeJobScheduler,
    clock: Clock,
) -> Runtime:
    tuned = settings.model_copy(
        update={"call_max_attempts": 3, "call_retry_delays_seconds": [600, 1800]}
    )
    return Runtime(settings=tuned, db=db, voice=voice, tier_verifier=verifier, jobs=jobs)


async def _new_deal(db: Database, **fields: Any) -> Deal:
    values: dict[str, Any] = {
        "customer_company": "Acme Corp",
        "ae_name": "Ravi Kumar",
        "ae_phone": "+919876543210",
        "ae_email": "ravi.ae@novacrm-demo.com",
        **fields,
    }
    async with db.session() as session:
        deal = Deal(
            gmail_message_id="m1", gmail_thread_id="t1", sender_email="ravi@x.com", **values
        )
        session.add(deal)
        await session.flush()
        return await transition(
            session, deal, DealStatus.AWAITING_TIER, actor="test", rationale="setup"
        )


async def _deal(db: Database, deal_id: str) -> Deal:
    async with db.session() as session:
        found = await session.get(Deal, deal_id)
    assert found is not None
    return found


async def _attempts(db: Database, deal_id: str) -> list[CallAttempt]:
    async with db.session() as session:
        rows = await session.exec(
            select(CallAttempt)
            .where(CallAttempt.deal_id == deal_id)
            .order_by(col(CallAttempt.attempt_no))
        )
        return list(rows.all())


def _message(payload: dict[str, Any]) -> VapiMessage:
    return VapiWebhook.model_validate({"message": payload}).message


def tool_call(
    call_id: str, tier: str, *, confirmed: bool = True, quote: str = "Enterprise"
) -> VapiMessage:
    return _message(
        {
            "type": "tool-calls",
            "call": {"id": call_id},
            "toolCallList": [
                {
                    "id": "tc_1",
                    "type": "function",
                    "function": {
                        "name": "record_plan_tier",
                        "arguments": {"tier": tier, "ae_confirmed": confirmed, "ae_quote": quote},
                    },
                }
            ],
        }
    )


def end_of_call(
    call_id: str, reason: str = "assistant-ended-call", transcript: str = TRANSCRIPT
) -> VapiMessage:
    return _message(
        {
            "type": "end-of-call-report",
            "endedReason": reason,
            "call": {"id": call_id},
            "artifact": {"transcript": transcript, "recordingUrl": "https://rec/1.wav"},
        }
    )


def verdict(tier: TierAnswer = TierAnswer.ENTERPRISE, quote: str = "Yes, correct") -> TierVerdict:
    return TierVerdict(tier=tier, ae_confirmed=True, evidence_quote=quote, rationale="clear")


async def _run_call(
    rt: Runtime,
    jobs: FakeJobScheduler,
    deal_id: str,
    *,
    tool: Callable[[str], VapiMessage] | None,
    reason: str = "assistant-ended-call",
) -> None:
    """One full attempt: dial, (tool call), end-of-call report, then the decision job."""
    await flow.start_call_attempt(rt, deal_id)
    call_id = (await _attempts(rt.db, deal_id))[-1].vapi_call_id
    assert call_id is not None
    if tool is not None:
        await flow.record_tool_calls(rt, tool(call_id))
    await flow.record_end_of_call(rt, end_of_call(call_id, reason))
    [job_id] = [j for j in jobs.jobs if j.startswith("process-call:")]
    await flow.process_call_result(rt, int(jobs.pop(job_id).args[0]))


# --- happy path --------------------------------------------------------------------


async def test_confirmed_enterprise_tier(
    rt: Runtime, jobs: FakeJobScheduler, voice: FakeVoiceCaller, verifier: FakeTierVerifier
) -> None:
    deal = await _new_deal(rt.db)
    verifier.verdicts.append(verdict())

    await _run_call(rt, jobs, deal.id, tool=lambda c: tool_call(c, "enterprise"))

    done = await _deal(rt.db, deal.id)
    assert done.status is DealStatus.TIER_CONFIRMED
    assert done.plan_tier is PlanTier.ENTERPRISE
    assert done.tier_source is TierSource.VOICE
    [placed] = voice.placed
    assert (placed.to_number, placed.attempt_no, placed.customer_company) == (
        "+919876543210",
        1,
        "Acme Corp",
    )
    [attempt] = await _attempts(rt.db, deal.id)
    assert attempt.outcome == CallOutcome.CONFIRMED
    assert (attempt.tool_tier, attempt.verifier_tier) == ("enterprise", "enterprise")
    assert attempt.recording_url == "https://rec/1.wav"
    assert verifier.called("verify") == [(TRANSCRIPT, "Acme Corp")]
    assert not jobs.jobs  # nothing else queued


async def test_decision_is_audited(
    rt: Runtime, jobs: FakeJobScheduler, verifier: FakeTierVerifier
) -> None:
    deal = await _new_deal(rt.db)
    verifier.verdicts.append(verdict())
    await _run_call(rt, jobs, deal.id, tool=lambda c: tool_call(c, "enterprise"))

    async with rt.db.session() as session:
        rows = (await session.exec(select(AuditLog).where(AuditLog.deal_id == deal.id))).all()
    actions = [r.action for r in rows]
    assert {"start_call", "call_started", "record_plan_tier", "decide_tier"} <= set(actions)
    decide = next(r for r in rows if r.action == "decide_tier")
    assert decide.status == "ok"
    assert decide.rationale is not None
    assert "agree on enterprise" in decide.rationale


# --- no answer, retries, escalation ------------------------------------------------


async def test_no_answer_schedules_retry_inside_window(
    rt: Runtime, jobs: FakeJobScheduler, verifier: FakeTierVerifier
) -> None:
    deal = await _new_deal(rt.db)

    await _run_call(rt, jobs, deal.id, tool=None, reason="customer-did-not-answer")

    assert (await _deal(rt.db, deal.id)).status is DealStatus.CALL_RETRY_WAIT
    retry = jobs.pop(f"call:{deal.id}:2")
    assert retry.job == flow.START_CALL_JOB
    assert retry.run_at == WED_10AM + timedelta(seconds=600)
    assert retry.args == (deal.id,)
    assert not verifier.called("verify")  # nobody answered: no LLM spend


async def test_retry_late_in_day_moves_to_next_morning(
    rt: Runtime, jobs: FakeJobScheduler, clock: Clock
) -> None:
    clock.now = datetime(2026, 10, 28, 18, 55, tzinfo=IST).astimezone(UTC)
    deal = await _new_deal(rt.db)

    await _run_call(rt, jobs, deal.id, tool=None, reason="customer-busy")

    retry = jobs.pop(f"call:{deal.id}:2")
    assert retry.run_at == datetime(2026, 10, 29, 9, 0, tzinfo=IST).astimezone(UTC)


async def test_three_unanswered_calls_escalate(rt: Runtime, jobs: FakeJobScheduler) -> None:
    deal = await _new_deal(rt.db)
    for attempt_no in (1, 2, 3):
        await _run_call(rt, jobs, deal.id, tool=None, reason="customer-did-not-answer")
        if attempt_no < 3:
            jobs.pop(f"call:{deal.id}:{attempt_no + 1}")

    done = await _deal(rt.db, deal.id)
    assert done.status is DealStatus.ESCALATED
    assert done.plan_tier is None  # never guessed
    assert len(await _attempts(rt.db, deal.id)) == 3
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]  # a human is told; no more calls


# --- ambiguity and the two keys ----------------------------------------------------


async def test_unclear_answer_is_retried_not_guessed(
    rt: Runtime, jobs: FakeJobScheduler, verifier: FakeTierVerifier
) -> None:
    deal = await _new_deal(rt.db)

    await _run_call(
        rt, jobs, deal.id, tool=lambda c: tool_call(c, "unclear", confirmed=False, quote="not sure")
    )

    assert (await _deal(rt.db, deal.id)).status is DealStatus.CALL_RETRY_WAIT
    [attempt] = await _attempts(rt.db, deal.id)
    assert attempt.outcome == CallOutcome.UNCLEAR
    assert not verifier.called("verify")


async def test_verifier_disagreeing_blocks_the_tier(
    rt: Runtime, jobs: FakeJobScheduler, verifier: FakeTierVerifier
) -> None:
    deal = await _new_deal(rt.db)
    verifier.verdicts.append(verdict(TierAnswer.GROWTH))

    await _run_call(rt, jobs, deal.id, tool=lambda c: tool_call(c, "enterprise"))

    done = await _deal(rt.db, deal.id)
    assert done.status is DealStatus.CALL_RETRY_WAIT
    assert done.plan_tier is None
    [attempt] = await _attempts(rt.db, deal.id)
    assert attempt.outcome_reason is not None
    assert "disagree" in attempt.outcome_reason


async def test_verifier_quote_not_in_transcript_blocks_the_tier(
    rt: Runtime, jobs: FakeJobScheduler, verifier: FakeTierVerifier
) -> None:
    deal = await _new_deal(rt.db)
    verifier.verdicts.append(verdict(quote="Enterprise, absolutely, sign us up"))

    await _run_call(rt, jobs, deal.id, tool=lambda c: tool_call(c, "enterprise"))

    assert (await _deal(rt.db, deal.id)).plan_tier is None


async def test_verifier_down_escalates_immediately(
    rt: Runtime, jobs: FakeJobScheduler, verifier: FakeTierVerifier
) -> None:
    deal = await _new_deal(rt.db)
    verifier.fail("verify", PermanentError("tier_verifier: HTTP 401"))

    await _run_call(rt, jobs, deal.id, tool=lambda c: tool_call(c, "enterprise"))

    assert (await _deal(rt.db, deal.id)).status is DealStatus.ESCALATED
    # No pointless re-call (the AE already answered); a human is told instead.
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]


# --- failures starting the call ----------------------------------------------------


@pytest.mark.parametrize("error", [PermanentError("401"), OutcomeUnknownError("timeout")])
async def test_call_that_cannot_start_counts_as_an_attempt(
    rt: Runtime, jobs: FakeJobScheduler, voice: FakeVoiceCaller, error: Exception
) -> None:
    deal = await _new_deal(rt.db)
    voice.fail("start_call", error)

    await flow.start_call_attempt(rt, deal.id)

    assert (await _deal(rt.db, deal.id)).status is DealStatus.CALL_RETRY_WAIT
    [attempt] = await _attempts(rt.db, deal.id)
    assert attempt.outcome == CallOutcome.CALL_ERROR
    assert f"call:{deal.id}:2" in jobs.jobs


async def test_deal_without_phone_is_escalated(
    rt: Runtime, voice: FakeVoiceCaller, jobs: FakeJobScheduler
) -> None:
    deal = await _new_deal(rt.db, ae_phone=None)

    await flow.start_call_attempt(rt, deal.id)

    assert (await _deal(rt.db, deal.id)).status is DealStatus.ESCALATED
    assert not voice.placed
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]


# --- calling window and idempotency ------------------------------------------------


async def test_outside_window_defers_without_calling(
    rt: Runtime, jobs: FakeJobScheduler, voice: FakeVoiceCaller, clock: Clock
) -> None:
    clock.now = datetime(2026, 10, 28, 22, 0, tzinfo=IST).astimezone(UTC)
    deal = await _new_deal(rt.db)

    await flow.start_call_attempt(rt, deal.id)

    assert not voice.placed
    assert (await _deal(rt.db, deal.id)).status is DealStatus.AWAITING_TIER
    deferred = jobs.pop(f"call:{deal.id}:1")
    assert deferred.run_at == datetime(2026, 10, 29, 9, 0, tzinfo=IST).astimezone(UTC)


async def test_start_job_running_twice_places_one_call(rt: Runtime, voice: FakeVoiceCaller) -> None:
    deal = await _new_deal(rt.db)
    await flow.start_call_attempt(rt, deal.id)
    await flow.start_call_attempt(rt, deal.id)  # deal is CALLING now: skipped
    assert len(voice.placed) == 1


async def test_duplicate_end_of_call_report_is_processed_once(
    rt: Runtime, jobs: FakeJobScheduler, verifier: FakeTierVerifier
) -> None:
    deal = await _new_deal(rt.db)
    verifier.verdicts.append(verdict())
    await flow.start_call_attempt(rt, deal.id)
    [attempt] = await _attempts(rt.db, deal.id)
    assert attempt.vapi_call_id is not None
    await flow.record_tool_calls(rt, tool_call(attempt.vapi_call_id, "enterprise"))

    await flow.record_end_of_call(rt, end_of_call(attempt.vapi_call_id))
    job = jobs.pop(f"process-call:{attempt.id}")
    await flow.record_end_of_call(rt, end_of_call(attempt.vapi_call_id))  # Vapi redelivers
    assert not jobs.jobs

    await flow.process_call_result(rt, int(job.args[0]))
    await flow.process_call_result(rt, int(job.args[0]))  # job retried after a crash
    assert len(verifier.called("verify")) == 1
    assert (await _deal(rt.db, deal.id)).status is DealStatus.TIER_CONFIRMED


async def test_webhook_racing_the_call_id_is_matched_by_metadata(
    rt: Runtime, voice: FakeVoiceCaller
) -> None:
    deal = await _new_deal(rt.db)
    await flow.start_call_attempt(rt, deal.id)
    async with rt.db.session() as session:  # simulate: call id not stored yet
        [attempt] = (await session.exec(select(CallAttempt))).all()
        attempt.vapi_call_id = None

    message = _message(
        {
            "type": "tool-calls",
            "call": {
                "id": "call_early",
                "assistantOverrides": {"metadata": {"deal_id": deal.id, "attempt_no": 1}},
            },
            "toolCallList": [
                {
                    "id": "tc_1",
                    "function": {
                        "name": "record_plan_tier",
                        "arguments": {"tier": "growth", "ae_confirmed": True, "ae_quote": "Growth"},
                    },
                }
            ],
        }
    )
    reply = await flow.record_tool_calls(rt, message)

    assert reply["results"][0]["result"] == flow.REPLY_CONFIRMED
    [attempt] = await _attempts(rt.db, deal.id)
    assert (attempt.vapi_call_id, attempt.tool_tier) == ("call_early", "growth")


async def test_invalid_tool_arguments_ask_the_assistant_to_retry(rt: Runtime) -> None:
    deal = await _new_deal(rt.db)
    await flow.start_call_attempt(rt, deal.id)
    [attempt] = await _attempts(rt.db, deal.id)
    assert attempt.vapi_call_id is not None

    reply = await flow.record_tool_calls(rt, tool_call(attempt.vapi_call_id, "platinum"))

    assert reply["results"][0]["result"].startswith("error:")
    [attempt] = await _attempts(rt.db, deal.id)
    assert attempt.tool_tier is None


async def test_human_retry_grants_exactly_one_more_call(
    rt: Runtime, jobs: FakeJobScheduler, voice: FakeVoiceCaller
) -> None:
    deal = await _new_deal(rt.db)
    for attempt_no in (1, 2, 3):
        await _run_call(rt, jobs, deal.id, tool=None, reason="customer-did-not-answer")
        if attempt_no < 3:
            jobs.pop(f"call:{deal.id}:{attempt_no + 1}")
    jobs.jobs.clear()  # drop the escalation notice

    _, queued = await resolve(
        rt,
        deal.id,
        Resolution(action=ResolveAction.RETRY_CALL, actor="Priya", reason="AE back now"),
    )
    assert queued == "call_queued"
    jobs.pop(f"call:{deal.id}:4")

    await _run_call(rt, jobs, deal.id, tool=None, reason="customer-busy")

    assert len(voice.placed) == 4
    assert (await _deal(rt.db, deal.id)).status is DealStatus.ESCALATED
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]
