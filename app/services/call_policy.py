"""Voice-call policy: when we may call, and what a finished call established. Pure: no I/O.

The tier is accepted only when two independent keys agree (guardrail G5):
1. the voice assistant's `record_plan_tier` tool call says the AE confirmed a tier on a
   read-back, and
2. the Tier Verifier agent, reading the full transcript afterwards, reaches the same tier,
   agrees the AE confirmed it, and quotes the AE verbatim from the transcript.

Anything else is UNCLEAR. There is no default tier.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo

from app.domain import CallOutcome, PlanTier, TierAnswer
from app.ports import TierVerdict
from app.services.grounding import is_grounded

# Vapi endedReason values meaning no human picked up.
# Source: docs.vapi.ai/calls/call-ended-reason
NO_ANSWER_REASONS: Final = frozenset({"customer-did-not-answer", "customer-busy", "voicemail"})
# A human was on the line and the call ended normally (whoever hung up).
NORMAL_END_REASONS: Final = frozenset(
    {
        "customer-ended-call",
        "assistant-ended-call",
        "assistant-said-end-call-phrase",
        "silence-timed-out",
        "exceeded-max-duration",
    }
)


@dataclass(frozen=True)
class CallDecision:
    outcome: CallOutcome
    tier: PlanTier | None
    reason: str


@dataclass(frozen=True)
class ToolReport:
    """What the voice assistant recorded via `record_plan_tier` during the call."""

    tier: TierAnswer
    ae_confirmed: bool
    ae_quote: str


# --- when to call ------------------------------------------------------------------


def next_call_time(
    earliest: datetime,
    *,
    window_start: time,
    window_end: time,
    days: Collection[int],
    timezone: str,
) -> datetime:
    """The first moment at or after `earliest` inside the AE's calling window (G11)."""
    tz = ZoneInfo(timezone)
    local = earliest.astimezone(tz)
    for offset in range(8):  # some allowed day is always within a week
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in days:
            continue
        opens = datetime.combine(day, window_start, tz)
        closes = datetime.combine(day, window_end, tz)
        if offset == 0 and local >= closes:
            continue
        return max(local, opens).astimezone(earliest.tzinfo)
    raise ValueError("no allowed calling day configured")  # pragma: no cover - validated config


# --- what the call established -----------------------------------------------------


def classify_without_verifier(
    ended_reason: str | None, tool: ToolReport | None
) -> CallDecision | None:
    """Decide from the call metadata alone, or return None when the verifier must run."""
    reason = ended_reason or "unknown"
    if reason in NO_ANSWER_REASONS:
        return CallDecision(CallOutcome.NO_ANSWER, None, f"AE not reached ({reason})")
    if tool is None:
        if reason in NORMAL_END_REASONS:
            return CallDecision(
                CallOutcome.UNCLEAR, None, f"call ended ({reason}) without a recorded tier"
            )
        return CallDecision(CallOutcome.CALL_ERROR, None, f"call failed ({reason})")
    if tool.tier is TierAnswer.UNCLEAR:
        return CallDecision(CallOutcome.UNCLEAR, None, "assistant recorded the tier as unclear")
    if not tool.ae_confirmed:
        return CallDecision(CallOutcome.UNCLEAR, None, "AE did not confirm the read-back")
    return None


def two_key_decision(tool: ToolReport, verdict: TierVerdict, transcript: str) -> CallDecision:
    if verdict.tier is TierAnswer.UNCLEAR:
        return CallDecision(CallOutcome.UNCLEAR, None, "verifier found the tier unclear")
    if verdict.tier is not tool.tier:
        return CallDecision(
            CallOutcome.UNCLEAR,
            None,
            f"keys disagree: assistant recorded {tool.tier}, verifier read {verdict.tier}",
        )
    if not verdict.ae_confirmed:
        return CallDecision(CallOutcome.UNCLEAR, None, "verifier saw no explicit AE confirmation")
    if not is_grounded(verdict.evidence_quote, transcript):
        return CallDecision(
            CallOutcome.UNCLEAR, None, "verifier's evidence quote is not in the transcript"
        )
    tier = verdict.tier.as_tier()
    assert tier is not None  # noqa: S101  # UNCLEAR was handled above
    return CallDecision(
        CallOutcome.CONFIRMED,
        tier,
        f"assistant and verifier agree on {tier}; AE said: {verdict.evidence_quote!r}",
    )
