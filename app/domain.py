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
