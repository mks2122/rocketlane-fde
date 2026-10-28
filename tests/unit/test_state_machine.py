from __future__ import annotations

import pytest
from sqlmodel import col, select

from app.db import Database
from app.domain import DealStatus as S
from app.domain import PlanTier
from app.errors import IllegalTransitionError, StaleDealError
from app.models import AuditLog, Deal
from app.orchestrator.state_machine import ALLOWED, TERMINAL, can_transition, transition

HAPPY_PATH = [
    S.AWAITING_TIER,
    S.CALLING,
    S.TIER_CONFIRMED,
    S.PROJECT_CREATED,
    S.COMPLETED,
]


def test_every_status_has_a_transition_entry() -> None:
    assert set(ALLOWED) == set(S)


def test_all_targets_are_known_statuses() -> None:
    for targets in ALLOWED.values():
        assert targets <= set(S)


def test_terminal_states() -> None:
    assert {S.IGNORED, S.COMPLETED} == TERMINAL


def test_project_cannot_be_created_without_confirmed_tier() -> None:
    # Guardrail G2, enforced structurally: the only way into PROJECT_CREATED
    # is from TIER_CONFIRMED, or from PARTIAL / ESCALATED which require a project already.
    sources = {s for s, targets in ALLOWED.items() if S.PROJECT_CREATED in targets}
    assert sources == {S.TIER_CONFIRMED, S.PARTIAL, S.ESCALATED}
    assert not can_transition(S.RECEIVED, S.PROJECT_CREATED)
    assert not can_transition(S.AWAITING_TIER, S.TIER_CONFIRMED)


def test_every_non_terminal_state_can_reach_a_human() -> None:
    # No dead ends: from any non-terminal state, ESCALATED or COMPLETED is reachable.
    def reachable(start: S) -> set[S]:
        seen: set[S] = set()
        stack = [start]
        while stack:
            for nxt in ALLOWED[stack.pop()]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    for status in set(S) - TERMINAL:
        assert reachable(status) & {S.ESCALATED, S.COMPLETED}, status


async def test_happy_path_transitions(db: Database, deal: Deal) -> None:
    async with db.session() as session:
        current = await session.get(Deal, deal.id)
        assert current is not None
        for target in HAPPY_PATH:
            current = await transition(session, current, target, actor="test", rationale="happy")

    async with db.session() as session:
        stored = await session.get(Deal, deal.id)
        assert stored is not None
        assert stored.status is S.COMPLETED
        assert stored.version == 1 + len(HAPPY_PATH)
        rows = (await session.exec(select(AuditLog).where(AuditLog.deal_id == deal.id))).all()
        assert [r.input_json["to"] for r in rows if r.input_json] == [s.value for s in HAPPY_PATH]


async def _transition_in_new_session(
    db: Database,
    deal_id: str,
    target: S,
    changes: dict[str, object] | None = None,
) -> Deal:
    async with db.session() as session:
        current = await session.get(Deal, deal_id)
        assert current is not None
        return await transition(
            session, current, target, actor="test", rationale="r", changes=changes
        )


async def test_illegal_transition_raises_and_changes_nothing(db: Database, deal: Deal) -> None:
    with pytest.raises(IllegalTransitionError):
        await _transition_in_new_session(db, deal.id, S.PROJECT_CREATED)

    async with db.session() as session:
        stored = await session.get(Deal, deal.id)
        assert stored is not None
        assert stored.status is S.RECEIVED
        assert stored.version == 1


async def test_transition_sets_fields_atomically(db: Database, deal: Deal) -> None:
    async with db.session() as session:
        current = await session.get(Deal, deal.id)
        assert current is not None
        for target in (S.AWAITING_TIER, S.CALLING):
            current = await transition(session, current, target, actor="test", rationale="setup")
        await transition(
            session,
            current,
            S.TIER_CONFIRMED,
            actor="voice",
            rationale="AE confirmed",
            changes={"plan_tier": PlanTier.ENTERPRISE},
        )

    async with db.session() as session:
        stored = await session.get(Deal, deal.id)
        assert stored is not None
        assert stored.plan_tier is PlanTier.ENTERPRISE


@pytest.mark.parametrize("field", ["status", "version", "id", "gmail_message_id", "not_a_field"])
async def test_transition_rejects_protected_or_unknown_fields(
    db: Database, deal: Deal, field: str
) -> None:
    with pytest.raises(ValueError, match="cannot set fields"):
        await _transition_in_new_session(db, deal.id, S.AWAITING_TIER, changes={field: "x"})


async def test_concurrent_update_is_detected(db: Database, deal: Deal) -> None:
    # Two workers load the same deal; the second write must fail, not overwrite.
    async with db.session() as s1:
        first = await s1.get(Deal, deal.id)
    async with db.session() as s2:
        second = await s2.get(Deal, deal.id)
    assert first is not None
    assert second is not None

    async with db.session() as session:
        await transition(session, first, S.AWAITING_TIER, actor="webhook", rationale="first")

    with pytest.raises(StaleDealError):
        async with db.session() as session:
            await transition(session, second, S.ESCALATED, actor="retry_job", rationale="second")

    async with db.session() as session:
        stored = await session.get(Deal, deal.id)
        assert stored is not None
        assert stored.status is S.AWAITING_TIER
        failed_audits = (
            await session.exec(select(AuditLog).where(col(AuditLog.rationale) == "second"))
        ).all()
        assert failed_audits == []


async def test_escalation_stamps_escalated_at(db: Database, deal: Deal) -> None:
    async with db.session() as session:
        escalated = await transition(session, deal, S.ESCALATED, actor="t", rationale="r")
    assert escalated.escalated_at is not None
    assert escalated.escalated_at == escalated.updated_at
    assert escalated.escalation_notified_at is None


async def test_escalated_at_cannot_be_set_by_callers(db: Database, deal: Deal) -> None:
    async with db.session() as session:
        with pytest.raises(ValueError, match="escalated_at"):
            await transition(
                session,
                deal,
                S.NEEDS_INFO,
                actor="t",
                rationale="r",
                changes={"escalated_at": None},
            )


def test_a_human_can_dismiss_an_escalated_deal() -> None:
    assert can_transition(S.ESCALATED, S.IGNORED)
    # ...but nothing else can skip straight to IGNORED once processing started.
    sources = {s for s, targets in ALLOWED.items() if S.IGNORED in targets}
    assert sources == {S.RECEIVED, S.ESCALATED}
