"""Audit log: one row per decision, with input, output, rationale and timing.

Use `audit_step` around every orchestrator step:

    async with audit_step(db, session, deal_id=deal.id, actor="intake_agent",
                          action="parse_email", input={"message_id": msg.id}) as step:
        result = ...
        step.output = result
        step.rationale = "all required fields present and grounded"

On success the row is added to the caller's session, so it commits atomically with
the state change. On failure the caller's transaction will roll back, so the error
row is written through a separate session and survives.
"""

from __future__ import annotations

import time
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, cast

import structlog
from agents.tracing import get_current_trace
from pydantic import BaseModel, SecretStr
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import Database
from app.models import AuditLog

log = structlog.get_logger(__name__)

MAX_STRING_CHARS = 2000
MAX_DEPTH = 6
REDACTED = "***"
SENSITIVE_KEYS = frozenset({"token", "api_key", "secret", "password", "authorization"})


def to_jsonable(value: Any, *, _depth: int = 0) -> Any:
    """Convert to JSON-safe data, masking secrets and truncating long text."""
    if _depth > MAX_DEPTH:
        return "<max depth>"
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, SecretStr):
        return REDACTED
    if isinstance(value, str):
        if len(value) > MAX_STRING_CHARS:
            return f"{value[:MAX_STRING_CHARS]}…<truncated {len(value) - MAX_STRING_CHARS} chars>"
        return value
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump(mode="python"), _depth=_depth + 1)
    if isinstance(value, Mapping):
        mapping = cast(Mapping[Any, Any], value)
        return {
            str(k): REDACTED
            if any(s in str(k).lower() for s in SENSITIVE_KEYS)
            else to_jsonable(v, _depth=_depth + 1)
            for k, v in mapping.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        items = cast(list[Any] | tuple[Any, ...] | set[Any] | frozenset[Any], value)
        return [to_jsonable(v, _depth=_depth + 1) for v in items]
    return to_jsonable(repr(value), _depth=_depth + 1)


def _as_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    data = to_jsonable(value)
    return cast(dict[str, Any], data) if isinstance(data, dict) else {"value": data}


def current_trace_id() -> str | None:
    current = get_current_trace()
    return current.trace_id if current is not None else None


@dataclass
class AuditStep:
    deal_id: str | None
    actor: str
    action: str
    input: Any = None
    output: Any = None
    rationale: str | None = None
    _started: float = field(default_factory=time.perf_counter)

    def to_row(self, *, status: str, error: str | None = None) -> AuditLog:
        return AuditLog(
            deal_id=self.deal_id,
            actor=self.actor,
            action=self.action,
            status=status,
            input_json=_as_object(self.input),
            output_json=_as_object(self.output),
            rationale=self.rationale,
            error=error,
            latency_ms=int((time.perf_counter() - self._started) * 1000),
            trace_id=current_trace_id(),
        )


@asynccontextmanager
async def audit_step(
    db: Database,
    session: AsyncSession,
    *,
    deal_id: str | None,
    actor: str,
    action: str,
    input: Any = None,  # mirrors the audit column name
) -> AsyncGenerator[AuditStep, None]:
    step = AuditStep(deal_id=deal_id, actor=actor, action=action, input=input)
    try:
        yield step
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        try:
            async with db.session() as independent:
                independent.add(step.to_row(status="error", error=error))
        except Exception:
            # Never mask the original failure because the audit write failed.
            log.exception("audit_write_failed", deal_id=deal_id, action=action)
        log.warning("step_failed", deal_id=deal_id, actor=actor, action=action, error=error)
        raise
    session.add(step.to_row(status="ok"))
    log.info("step_ok", deal_id=deal_id, actor=actor, action=action, rationale=step.rationale)


def record(
    session: AsyncSession,
    *,
    deal_id: str | None,
    actor: str,
    action: str,
    input: Any = None,
    output: Any = None,
    rationale: str | None = None,
) -> None:
    """Add a one-off audit row (no timing) to the caller's transaction."""
    step = AuditStep(
        deal_id=deal_id, actor=actor, action=action, input=input, output=output, rationale=rationale
    )
    session.add(step.to_row(status="ok"))
