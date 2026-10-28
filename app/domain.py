"""Core domain enums shared across layers."""

from enum import StrEnum


class PlanTier(StrEnum):
    ENTERPRISE = "enterprise"
    GROWTH = "growth"


class TierSource(StrEnum):
    VOICE = "voice"
    HUMAN_OVERRIDE = "human_override"


class DealStatus(StrEnum):
    RECEIVED = "received"
    IGNORED = "ignored"
    NEEDS_INFO = "needs_info"
    AWAITING_TIER = "awaiting_tier"
    CALLING = "calling"
    CALL_RETRY_WAIT = "call_retry_wait"
    TIER_CONFIRMED = "tier_confirmed"
    DUPLICATE_HOLD = "duplicate_hold"
    RL_RETRY_WAIT = "rl_retry_wait"
    PROJECT_CREATED = "project_created"
    PARTIAL = "partial"
    COMPLETED = "completed"
    ESCALATED = "escalated"


class TierAnswer(StrEnum):
    """What a call established about the tier. UNCLEAR is a first-class answer, never a guess."""

    ENTERPRISE = "enterprise"
    GROWTH = "growth"
    UNCLEAR = "unclear"

    def as_tier(self) -> PlanTier | None:
        return None if self is TierAnswer.UNCLEAR else PlanTier(self.value)


class CallOutcome(StrEnum):
    CONFIRMED = "confirmed"  # both keys agree on a tier
    NO_ANSWER = "no_answer"  # did not answer, busy, voicemail
    UNCLEAR = "unclear"  # answered, but the tier wasn't established
    CALL_ERROR = "call_error"  # the call itself failed (provider/pipeline/start error)
