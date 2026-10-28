from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.domain import CallOutcome, PlanTier, TierAnswer
from app.ports import TierVerdict
from app.services.call_policy import (
    ToolReport,
    classify_without_verifier,
    next_call_time,
    two_key_decision,
)
from app.services.grounding import is_grounded

IST = ZoneInfo("Asia/Kolkata")
WEEKDAYS = [0, 1, 2, 3, 4]


def _ist(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in Oct 2026 (the 28th is a Wednesday), as UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=IST).astimezone(UTC)


def _next(earliest: datetime) -> datetime:
    return next_call_time(
        earliest,
        window_start=time(9, 0),
        window_end=time(19, 0),
        days=WEEKDAYS,
        timezone="Asia/Kolkata",
    )


# --- calling window (G11) ----------------------------------------------------------


def test_inside_window_calls_now() -> None:
    assert _next(_ist(28, 10, 30)) == _ist(28, 10, 30)


def test_before_window_waits_for_opening() -> None:
    assert _next(_ist(28, 7, 0)) == _ist(28, 9, 0)


def test_after_window_moves_to_next_day() -> None:
    assert _next(_ist(28, 19, 0)) == _ist(29, 9, 0)


def test_friday_evening_moves_to_monday() -> None:
    monday_open = datetime(2026, 11, 2, 9, 0, tzinfo=IST).astimezone(UTC)
    assert _next(_ist(30, 20, 0)) == monday_open


def test_result_keeps_callers_timezone() -> None:
    assert _next(_ist(28, 7, 0)).tzinfo == UTC


# --- classification before the verifier ----------------------------------------------


@pytest.mark.parametrize("reason", ["customer-did-not-answer", "customer-busy", "voicemail"])
def test_not_reached_is_no_answer(reason: str) -> None:
    tool = ToolReport(TierAnswer.ENTERPRISE, ae_confirmed=True, ae_quote="enterprise")
    decision = classify_without_verifier(reason, tool)
    assert decision is not None
    assert decision.outcome is CallOutcome.NO_ANSWER


def test_hangup_without_tool_call_is_unclear() -> None:
    decision = classify_without_verifier("customer-ended-call", None)
    assert decision is not None
    assert decision.outcome is CallOutcome.UNCLEAR


@pytest.mark.parametrize(
    "reason", ["twilio-failed-to-connect-call", "openai-llm-failed", "pipeline-error-x", None]
)
def test_provider_failure_without_tool_call_is_call_error(reason: str | None) -> None:
    decision = classify_without_verifier(reason, None)
    assert decision is not None
    assert decision.outcome is CallOutcome.CALL_ERROR


def test_tool_unclear_is_unclear() -> None:
    decision = classify_without_verifier(
        "assistant-ended-call", ToolReport(TierAnswer.UNCLEAR, ae_confirmed=False, ae_quote="")
    )
    assert decision is not None
    assert decision.outcome is CallOutcome.UNCLEAR


def test_unconfirmed_read_back_is_unclear() -> None:
    decision = classify_without_verifier(
        "assistant-ended-call", ToolReport(TierAnswer.GROWTH, ae_confirmed=False, ae_quote="growth")
    )
    assert decision is not None
    assert "did not confirm" in decision.reason


def test_confirmed_tool_report_needs_the_verifier() -> None:
    tool = ToolReport(TierAnswer.GROWTH, ae_confirmed=True, ae_quote="growth")
    assert classify_without_verifier("assistant-ended-call", tool) is None


# --- two-key decision (G5) ---------------------------------------------------------

TRANSCRIPT = (
    "AI: Which plan did Acme sign up for: Enterprise or Growth?\n"
    "User: They went with the Enterprise plan.\n"
    "AI: Just to confirm: Acme is on the Enterprise plan, correct?\n"
    "User: Yes, that's right."
)
TOOL = ToolReport(TierAnswer.ENTERPRISE, ae_confirmed=True, ae_quote="the Enterprise plan")


def _verdict(
    tier: TierAnswer = TierAnswer.ENTERPRISE,
    *,
    confirmed: bool = True,
    quote: str = "They went with the Enterprise plan",
) -> TierVerdict:
    return TierVerdict(tier=tier, ae_confirmed=confirmed, evidence_quote=quote, rationale="r")


def test_both_keys_agree_confirms_tier() -> None:
    decision = two_key_decision(TOOL, _verdict(), TRANSCRIPT)
    assert decision.outcome is CallOutcome.CONFIRMED
    assert decision.tier is PlanTier.ENTERPRISE


def test_keys_disagree_is_unclear() -> None:
    decision = two_key_decision(TOOL, _verdict(TierAnswer.GROWTH), TRANSCRIPT)
    assert decision.outcome is CallOutcome.UNCLEAR
    assert decision.tier is None
    assert "disagree" in decision.reason


def test_verifier_unclear_is_unclear() -> None:
    assert two_key_decision(TOOL, _verdict(TierAnswer.UNCLEAR), TRANSCRIPT).tier is None


def test_verifier_sees_no_confirmation() -> None:
    decision = two_key_decision(TOOL, _verdict(confirmed=False), TRANSCRIPT)
    assert decision.outcome is CallOutcome.UNCLEAR


def test_invented_quote_is_rejected() -> None:
    decision = two_key_decision(TOOL, _verdict(quote="definitely Enterprise, no doubt"), TRANSCRIPT)
    assert decision.outcome is CallOutcome.UNCLEAR
    assert "not in the transcript" in decision.reason


@pytest.mark.parametrize(
    ("quote", "grounded"),
    [
        ("they went with the ENTERPRISE plan!", True),  # case and punctuation don't matter
        ("went   with\nthe Enterprise", True),
        ("Enterprise plan, correct", True),
        ("ok", False),  # too short to mean anything
        ("", False),
        ("Growth plan", False),
    ],
)
def test_grounding(quote: str, grounded: bool) -> None:
    assert is_grounded(quote, TRANSCRIPT) is grounded
