"""Response models for the admin API. Built from DB rows, never returned raw."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.domain import DealStatus, PlanTier, TierSource


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class DealSummary(_Out):
    id: str
    status: DealStatus
    customer_company: str | None
    ae_name: str | None
    plan_tier: PlanTier | None
    tier_source: TierSource | None
    created_at: datetime
    updated_at: datetime
    escalated_at: datetime | None


class CallAttemptOut(_Out):
    attempt_no: int
    vapi_call_id: str | None
    started_at: datetime
    ended_at: datetime | None
    ended_reason: str | None
    tool_tier: str | None
    tool_confirmed: bool | None
    tool_quote: str | None
    verifier_tier: str | None
    verifier_confirmed: bool | None
    verifier_quote: str | None
    outcome: str | None
    outcome_reason: str | None
    recording_url: str | None
    transcript: str | None


class DealDetail(DealSummary):
    version: int
    gmail_message_id: str
    gmail_thread_id: str
    sender_email: str
    contact_name: str | None
    contact_email: str | None
    ae_email: str | None
    ae_phone: str | None
    sf_opportunity_url: str | None
    rocketlane_project_id: str | None
    slack_channel_id: str | None
    escalation_notified_at: datetime | None
    call_attempts: list[CallAttemptOut]


class AuditEntry(_Out):
    ts: datetime
    actor: str
    action: str
    status: str
    input_json: dict[str, Any] | None
    output_json: dict[str, Any] | None
    rationale: str | None
    error: str | None
    latency_ms: int | None
    trace_id: str | None


class SpanOut(_Out):
    span_id: str
    parent_id: str | None
    span_type: str
    started_at: str | None
    ended_at: str | None
    data_json: dict[str, Any] | None
    error_json: dict[str, Any] | None


class TraceOut(BaseModel):
    trace_id: str
    name: str
    started_at: datetime
    ended_at: datetime | None
    spans: list[SpanOut]
