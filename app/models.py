"""Persistence models.

All timestamps are timezone-aware UTC in Python. SQLite has no timezone type, so
`UTCDateTime` stores naive UTC and re-attaches UTC on read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Column, DateTime, Dialect, UniqueConstraint
from sqlalchemy.types import TypeDecorator
from sqlmodel import Field, SQLModel

from app.domain import DealStatus, PlanTier, TierSource


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime given; use timezone-aware UTC")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


def _ts(*, index: bool = False) -> Any:
    return Field(
        default_factory=utcnow, sa_column=Column(UTCDateTime(), nullable=False, index=index)
    )


def _optional_ts() -> Any:
    return Field(default=None, sa_column=Column(UTCDateTime(), nullable=True))


def _new_deal_id() -> str:
    return f"d_{uuid4().hex[:12]}"


class Deal(SQLModel, table=True):
    __tablename__ = "deals"  # pyright: ignore[reportAssignmentType]

    id: str = Field(default_factory=_new_deal_id, primary_key=True)
    gmail_message_id: str = Field(unique=True, index=True)
    gmail_thread_id: str = Field(index=True)
    status: DealStatus = Field(default=DealStatus.RECEIVED, index=True)
    version: int = Field(default=1, ge=1)

    sender_email: str
    customer_company: str | None = None
    contact_name: str | None = None
    contact_email: str | None = None
    ae_name: str | None = None
    ae_email: str | None = None
    ae_phone: str | None = None
    sf_opportunity_url: str | None = None

    plan_tier: PlanTier | None = None
    tier_source: TierSource | None = None
    rocketlane_project_id: str | None = None
    slack_channel_id: str | None = None

    # Set by transition() on every move to ESCALATED; a notice is owed until
    # escalation_notified_at catches up with it.
    escalated_at: datetime | None = _optional_ts()
    escalation_notified_at: datetime | None = _optional_ts()

    created_at: datetime = _ts()
    updated_at: datetime = _ts()


class CallAttempt(SQLModel, table=True):
    __tablename__ = "call_attempts"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (UniqueConstraint("deal_id", "attempt_no"),)

    id: int | None = Field(default=None, primary_key=True)
    deal_id: str = Field(foreign_key="deals.id", index=True)
    attempt_no: int = Field(ge=1)
    vapi_call_id: str | None = Field(default=None, unique=True)
    ended_reason: str | None = None
    tool_tier: str | None = None
    tool_confirmed: bool | None = None
    tool_quote: str | None = None
    verifier_tier: str | None = None
    verifier_confirmed: bool | None = None
    verifier_quote: str | None = None
    outcome: str | None = None
    outcome_reason: str | None = None
    transcript: str | None = None
    recording_url: str | None = None
    started_at: datetime = _ts()
    ended_at: datetime | None = _optional_ts()


class AuditLog(SQLModel, table=True):
    __tablename__ = "audit_log"  # pyright: ignore[reportAssignmentType]

    id: int | None = Field(default=None, primary_key=True)
    ts: datetime = _ts(index=True)
    deal_id: str | None = Field(default=None, index=True)
    actor: str
    action: str
    status: str  # "ok" | "error"
    input_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    output_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    rationale: str | None = None
    error: str | None = None
    latency_ms: int | None = None
    trace_id: str | None = Field(default=None, index=True)


class AgentTrace(SQLModel, table=True):
    __tablename__ = "agent_traces"  # pyright: ignore[reportAssignmentType]

    trace_id: str = Field(primary_key=True)
    name: str
    group_id: str | None = Field(default=None, index=True)
    metadata_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    started_at: datetime = _ts()
    ended_at: datetime | None = _optional_ts()


class AgentSpan(SQLModel, table=True):
    __tablename__ = "agent_spans"  # pyright: ignore[reportAssignmentType]

    span_id: str = Field(primary_key=True)
    trace_id: str = Field(index=True)
    parent_id: str | None = None
    span_type: str
    started_at: str | None = None  # ISO strings exactly as emitted by the SDK
    ended_at: str | None = None
    data_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error_json: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))


class WebhookEvent(SQLModel, table=True):
    __tablename__ = "webhook_events"  # pyright: ignore[reportAssignmentType]
    __table_args__ = (UniqueConstraint("call_id", "message_type"),)

    id: int | None = Field(default=None, primary_key=True)
    call_id: str
    message_type: str
    received_at: datetime = _ts()


class RoutingState(SQLModel, table=True):
    __tablename__ = "routing_state"  # pyright: ignore[reportAssignmentType]

    tier: PlanTier = Field(primary_key=True)
    cursor: int = Field(default=0, ge=0)
