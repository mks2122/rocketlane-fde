"""Project step: create the onboarding project in Rocketlane from the tier's template.

    TIER_CONFIRMED ─ create_project_for ─┬─ our marker already exists  ─► PROJECT_CREATED
                                         ├─ same customer, active      ─► DUPLICATE_HOLD → ESCALATED
                                         ├─ created                    ─► PROJECT_CREATED
                                         ├─ Rocketlane down/ambiguous  ─► RL_RETRY_WAIT (+ job)
                                         └─ 4xx, auth, no template, out of retries ─► ESCALATED

Order matters for idempotency. The marker check runs on every attempt, before any create:
a create that timed out but succeeded is found and adopted, never duplicated. The CSM is
chosen once and stored on the deal, so retries reuse the same person.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Final
from zoneinfo import ZoneInfo

import structlog
from agents import trace
from sqlmodel import select

from app.domain import DealStatus
from app.errors import ConfigError, IntegrationError, RetryableError
from app.models import AuditLog, Deal, utcnow
from app.orchestrator.communication import queue_channel_setup
from app.orchestrator.escalation import escalate, queue_escalation_notice
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition, update_fields
from app.ports import ProjectRef
from app.services.audit import audit_step
from app.services.routing import (
    build_project,
    find_duplicates,
    find_own_project,
    next_csm_index,
    pick_csm,
)

log = structlog.get_logger(__name__)

ACTOR: Final = "project_agent"
PROJECT_JOB: Final = "app.orchestrator.jobs:project_job"


def queue_project_creation(
    rt: Runtime, deal_id: str, *, attempt: int = 1, delay_s: int = 0
) -> None:
    rt.jobs.schedule(
        PROJECT_JOB,
        run_at=utcnow() + timedelta(seconds=delay_s),
        args=[deal_id, str(attempt)],
        job_id=f"project:{deal_id}:{attempt}",
    )


async def create_project_for(rt: Runtime, deal_id: str, attempt: int = 1) -> None:
    """Create (or adopt) the deal's Rocketlane project. Safe to run twice."""
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
        if deal is not None and deal.status is DealStatus.RL_RETRY_WAIT:
            deal = await transition(
                session,
                deal,
                DealStatus.TIER_CONFIRMED,
                actor=ACTOR,
                rationale=f"retrying project creation (attempt {attempt})",
            )
    if deal is None or deal.status is not DealStatus.TIER_CONFIRMED:
        log.info("project_skipped", deal_id=deal_id, status=deal.status if deal else None)
        return

    with (
        structlog.contextvars.bound_contextvars(deal_id=deal_id),
        trace("project_creation", group_id=deal_id),
    ):
        if rt.projects is None or rt.templates is None:
            await _escalate(rt, deal, "project creation is not configured (Rocketlane API key)")
            return
        tier = deal.plan_tier
        if tier is None:  # pragma: no cover - TIER_CONFIRMED always carries a tier
            await _escalate(rt, deal, "deal is tier-confirmed but has no tier")
            return

        try:
            deal = await _ensure_csm(rt, deal)
            existing = await rt.projects.list_projects()
        except RetryableError as exc:
            await _retry_later(rt, deal, attempt, f"could not list Rocketlane projects: {exc}")
            return
        except IntegrationError as exc:
            await _escalate(rt, deal, f"could not list Rocketlane projects: {exc}")
            return

        own = find_own_project(existing, deal.id)
        if own is not None:
            await _mark_created(rt, deal, own, attempt, adopted=True)
            return
        duplicates = find_duplicates(
            existing, customer_company=deal.customer_company or "", deal_id=deal.id
        )
        if duplicates:
            await _hold_duplicate(rt, deal, duplicates)
            return

        try:
            assert deal.csm_email is not None  # noqa: S101  # set by _ensure_csm
            request = build_project(
                deal,
                tier=tier,
                config=rt.templates,
                csm_email=deal.csm_email,
                start=_today(rt),
            )
        except ConfigError as exc:
            await _escalate(rt, deal, f"cannot create the project: {exc}")
            return

        try:
            created = await rt.projects.create_project(request)
        except RetryableError as exc:
            # Includes OutcomeUnknownError: the next attempt looks for our marker first.
            await _retry_later(rt, deal, attempt, f"Rocketlane create failed: {exc}")
            return
        except IntegrationError as exc:
            await _escalate(rt, deal, f"Rocketlane rejected the project: {exc}")
            return
        await _mark_created(
            rt, deal, created, attempt, adopted=False, template_id=request.template_id
        )


def _today(rt: Runtime) -> date:
    """Project start date: today in the business timezone (not UTC)."""
    return utcnow().astimezone(ZoneInfo(rt.settings.call_timezone)).date()


async def _ensure_csm(rt: Runtime, deal: Deal) -> Deal:
    if deal.csm_email is not None or deal.plan_tier is None or rt.templates is None:
        return deal
    tier_config = rt.templates.for_tier(deal.plan_tier)
    async with rt.db.session() as session:
        index = await next_csm_index(session, deal.plan_tier)
        csm = pick_csm(tier_config, index)
        return await update_fields(
            session,
            deal,
            actor=ACTOR,
            rationale=f"{tier_config.csm_mode} CSM for {deal.plan_tier}: {csm}",
            changes={"csm_email": csm},
        )


async def _mark_created(
    rt: Runtime,
    deal: Deal,
    project: ProjectRef,
    attempt: int,
    *,
    adopted: bool,
    template_id: int | None = None,
) -> None:
    why = (
        "found the project an earlier attempt created (marker match); adopted it"
        if adopted
        else f"created from template {template_id} for the {deal.plan_tier} tier"
    )
    async with (
        rt.db.session() as session,
        audit_step(
            rt.db,
            session,
            deal_id=deal.id,
            actor=ACTOR,
            action="create_project",
            input={"attempt": attempt, "tier": deal.plan_tier, "csm": deal.csm_email},
        ) as step,
    ):
        await transition(
            session,
            deal,
            DealStatus.PROJECT_CREATED,
            actor=ACTOR,
            rationale=why,
            changes={"rocketlane_project_id": project.project_id},
        )
        step.output = {"project_id": project.project_id, "name": project.name, "adopted": adopted}
        step.rationale = why
    log.info("project_created", project_id=project.project_id, adopted=adopted)
    queue_channel_setup(rt, deal.id)  # after commit: the channel job sees PROJECT_CREATED


async def _hold_duplicate(rt: Runtime, deal: Deal, duplicates: list[ProjectRef]) -> None:
    listed = "; ".join(f"{p.name} (id {p.project_id})" for p in duplicates)
    reason = f"an active project already exists for {deal.customer_company}: {listed}"
    # Hold and escalate in one transaction: the hold is never left without a human.
    async with rt.db.session() as session:
        held = await transition(
            session, deal, DealStatus.DUPLICATE_HOLD, actor=ACTOR, rationale=reason
        )
        await escalate(
            session, held, actor=ACTOR, reason=f"possible duplicate onboarding: {reason}"
        )
    queue_escalation_notice(rt, deal.id)


async def _retry_later(rt: Runtime, deal: Deal, attempt: int, reason: str) -> None:
    delays = rt.settings.rocketlane_retry_delays_seconds
    if attempt > len(delays):
        await _escalate(
            rt, deal, f"Rocketlane still failing after {attempt} attempts; last: {reason}"
        )
        return
    delay = delays[attempt - 1]
    async with rt.db.session() as session:
        await transition(
            session,
            deal,
            DealStatus.RL_RETRY_WAIT,
            actor=ACTOR,
            rationale=f"{reason}; attempt {attempt + 1} in {delay}s",
        )
    queue_project_creation(rt, deal.id, attempt=attempt + 1, delay_s=delay)
    log.warning("project_retry_scheduled", attempt=attempt, delay_s=delay, reason=reason)


async def _escalate(rt: Runtime, deal: Deal, reason: str) -> None:
    async with rt.db.session() as session:
        await escalate(session, deal, actor=ACTOR, reason=reason)
    queue_escalation_notice(rt, deal.id)


async def attempts_so_far(rt: Runtime, deal_id: str) -> int:
    """How many project attempts already failed retryably (moves into RL_RETRY_WAIT)."""
    async with rt.db.session() as session:
        rows = await session.exec(
            select(AuditLog).where(AuditLog.deal_id == deal_id, AuditLog.action == "transition")
        )
        return sum(
            1 for r in rows if (r.input_json or {}).get("to") == DealStatus.RL_RETRY_WAIT.value
        )
