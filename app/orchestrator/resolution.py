"""Human resolution of an escalated deal (`POST /deals/{id}/resolve`).

Only ESCALATED deals can be resolved. Every resolution names the person and their
reason; both land in the audit log as `human:<actor>`.

| action           | deal becomes   | then                                               |
|------------------|----------------|----------------------------------------------------|
| confirm_tier     | TIER_CONFIRMED | tier recorded as a human override (plan_tier given) |
| retry_call       | AWAITING_TIER  | one more call is queued (inside the calling window) |
| reprocess_intake | RECEIVED       | intake re-reads the email thread (P6)              |
| retry_channel    | PROJECT_CREATED| the Slack channel is set up again (project exists)  |
| dismiss          | IGNORED        | nothing: closed                                    |

A human retry grants exactly one more call: if it fails too, the deal is escalated again.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain import DealStatus, PlanTier, TierSource
from app.errors import IllegalTransitionError, NotFoundError
from app.models import Deal
from app.orchestrator.progress import Advance, advance
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.services.audit import audit_step


class ResolveAction(StrEnum):
    CONFIRM_TIER = "confirm_tier"
    RETRY_CALL = "retry_call"
    REPROCESS_INTAKE = "reprocess_intake"
    RETRY_CHANNEL = "retry_channel"
    DISMISS = "dismiss"


TARGET: Final = {
    ResolveAction.CONFIRM_TIER: DealStatus.TIER_CONFIRMED,
    ResolveAction.RETRY_CALL: DealStatus.AWAITING_TIER,
    ResolveAction.REPROCESS_INTAKE: DealStatus.RECEIVED,
    ResolveAction.RETRY_CHANNEL: DealStatus.PROJECT_CREATED,
    ResolveAction.DISMISS: DealStatus.IGNORED,
}


class Resolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: ResolveAction
    actor: str = Field(
        min_length=1, max_length=100, description="Who is resolving (name or email)."
    )
    reason: str = Field(min_length=5, max_length=1000, description="Why; stored in the audit log.")
    plan_tier: PlanTier | None = Field(default=None, description="Required for confirm_tier.")

    @model_validator(mode="after")
    def _tier_only_when_confirming(self) -> Self:
        confirming = self.action is ResolveAction.CONFIRM_TIER
        if confirming and self.plan_tier is None:
            raise ValueError("plan_tier is required for confirm_tier")
        if not confirming and self.plan_tier is not None:
            raise ValueError("plan_tier is only allowed with confirm_tier")
        return self


async def resolve(rt: Runtime, deal_id: str, resolution: Resolution) -> tuple[Deal, Advance]:
    """Apply a human resolution. Raises NotFoundError, IllegalTransitionError or
    StaleDealError (someone else changed the deal meanwhile)."""
    actor = f"human:{resolution.actor}"
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
        if deal is None:
            raise NotFoundError(f"deal {deal_id} not found")
        if deal.status is not DealStatus.ESCALATED:
            raise IllegalTransitionError(
                f"only escalated deals can be resolved; {deal_id} is {deal.status}"
            )
        if resolution.action is ResolveAction.RETRY_CHANNEL and deal.rocketlane_project_id is None:
            raise IllegalTransitionError(f"{deal_id} has no Rocketlane project; can't retry Slack")
        changes: dict[str, Any] = {}
        if resolution.action is ResolveAction.CONFIRM_TIER:
            changes = {"plan_tier": resolution.plan_tier, "tier_source": TierSource.HUMAN_OVERRIDE}
        async with audit_step(
            rt.db, session, deal_id=deal_id, actor=actor, action="resolve", input=resolution
        ) as step:
            deal = await transition(
                session,
                deal,
                TARGET[resolution.action],
                actor=actor,
                rationale=resolution.reason,
                changes=changes,
            )
            step.output = {"status": deal.status}
            step.rationale = resolution.reason
    # After commit, so any queued job sees the resolved state.
    return deal, await advance(rt, deal)
