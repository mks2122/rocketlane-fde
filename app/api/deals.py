"""Admin API over deals (X-Admin-Token): state, decision timeline and agent traces.

Debug order for a deal: GET /deals/{id} → /audit (which step, why) → /traces (what the
agents saw and returned) → the Vapi dashboard (call recording).
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlmodel import col, select

from app.api.deps import AdminDep, RuntimeDep
from app.api.schemas import (
    ActionResult,
    AuditEntry,
    CallAttemptOut,
    DealDetail,
    DealSummary,
    SpanOut,
    TraceOut,
)
from app.domain import DealStatus
from app.errors import IllegalTransitionError, NotFoundError, StaleDealError
from app.models import AgentSpan, AgentTrace, AuditLog, CallAttempt, Deal
from app.orchestrator.resolution import Resolution, resolve

router = APIRouter(prefix="/deals", tags=["deals"])


async def _get_deal(rt: RuntimeDep, deal_id: str) -> Deal:
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"deal {deal_id} not found")
    return deal


@router.get("", response_model=list[DealSummary])
async def list_deals(
    rt: RuntimeDep,
    _: AdminDep,
    status_: Annotated[DealStatus | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[DealSummary]:
    query = select(Deal).order_by(col(Deal.created_at).desc()).limit(limit)
    if status_ is not None:
        query = query.where(Deal.status == status_)
    async with rt.db.session() as session:
        deals = (await session.exec(query)).all()
    return [DealSummary.model_validate(d) for d in deals]


@router.get("/{deal_id}", response_model=DealDetail)
async def get_deal(deal_id: str, rt: RuntimeDep, _: AdminDep) -> DealDetail:
    deal = await _get_deal(rt, deal_id)
    async with rt.db.session() as session:
        attempts = (
            await session.exec(
                select(CallAttempt)
                .where(CallAttempt.deal_id == deal_id)
                .order_by(col(CallAttempt.attempt_no))
            )
        ).all()
    return DealDetail.model_validate(
        {
            **deal.model_dump(),
            "call_attempts": [CallAttemptOut.model_validate(a) for a in attempts],
        }
    )


@router.get("/{deal_id}/audit", response_model=list[AuditEntry])
async def get_audit(deal_id: str, rt: RuntimeDep, _: AdminDep) -> list[AuditEntry]:
    await _get_deal(rt, deal_id)
    async with rt.db.session() as session:
        rows = (
            await session.exec(
                select(AuditLog).where(AuditLog.deal_id == deal_id).order_by(col(AuditLog.id))
            )
        ).all()
    return [AuditEntry.model_validate(r) for r in rows]


@router.get("/{deal_id}/traces", response_model=list[TraceOut])
async def get_traces(deal_id: str, rt: RuntimeDep, _: AdminDep) -> list[TraceOut]:
    await _get_deal(rt, deal_id)
    async with rt.db.session() as session:
        traces = (
            await session.exec(
                select(AgentTrace)
                .where(AgentTrace.group_id == deal_id)
                .order_by(col(AgentTrace.started_at))
            )
        ).all()
        trace_ids = [t.trace_id for t in traces]
        spans = (
            await session.exec(select(AgentSpan).where(col(AgentSpan.trace_id).in_(trace_ids)))
        ).all()
    by_trace: dict[str, list[SpanOut]] = {}
    for span in spans:
        by_trace.setdefault(span.trace_id, []).append(SpanOut.model_validate(span))
    return [
        TraceOut(
            trace_id=t.trace_id,
            name=t.name,
            started_at=t.started_at,
            ended_at=t.ended_at,
            spans=by_trace.get(t.trace_id, []),
        )
        for t in traces
    ]


@router.post("/{deal_id}/resolve", response_model=ActionResult)
async def resolve_deal(deal_id: str, body: Resolution, rt: RuntimeDep, _: AdminDep) -> ActionResult:
    """Resolve an escalated deal. See `orchestrator/resolution.py` for the actions."""
    with _domain_errors_as_http():
        deal, queued = await resolve(rt, deal_id, body)
    return ActionResult(deal=DealSummary.model_validate(deal), next=queued)


@contextmanager
def _domain_errors_as_http() -> Generator[None, None, None]:
    try:
        yield
    except NotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (IllegalTransitionError, StaleDealError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
