"""Project creation end to end with a fake Rocketlane: Part 4's edge cases.

Template accuracy, Rocketlane down → retries → escalation, a create that timed out but
succeeded, an existing project for the customer, permanent errors, and idempotency.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlmodel import select

from app.config import Settings, TemplatesConfig
from app.db import Database
from app.domain import DealStatus, PlanTier
from app.errors import OutcomeUnknownError, PermanentError, RetryableError
from app.integrations.fakes import (
    FakeJobScheduler,
    FakeProjectStore,
    FakeTierVerifier,
    FakeVoiceCaller,
)
from app.models import AuditLog, Deal
from app.orchestrator.escalation import escalate
from app.orchestrator.progress import advance
from app.orchestrator.project import create_project_for
from app.orchestrator.resolution import Resolution, ResolveAction, resolve
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.ports import ProjectRef

TEMPLATES = TemplatesConfig.model_validate(
    {
        "enterprise": {
            "rocketlane_template_id": 111,
            "duration_days": 30,
            "csm_mode": "dedicated",
            "csm_pool": ["anita@x.com", "vikram@x.com"],
            "project_owner": "priya@x.com",
        },
        "growth": {
            "rocketlane_template_id": 222,
            "duration_days": 14,
            "csm_mode": "pooled",
            "csm_pool": ["pod@x.com"],
            "project_owner": "priya@x.com",
        },
    }
)


@pytest.fixture
def jobs() -> FakeJobScheduler:
    return FakeJobScheduler()


@pytest.fixture
def rt(
    settings: Settings, db: Database, jobs: FakeJobScheduler, projects: FakeProjectStore
) -> Runtime:
    return Runtime(
        settings=settings.model_copy(update={"rocketlane_retry_delays_seconds": [60, 300, 900]}),
        db=db,
        voice=FakeVoiceCaller(),
        tier_verifier=FakeTierVerifier(),
        jobs=jobs,
        projects=projects,
        templates=TEMPLATES,
    )


_counter = iter(range(1, 10_000))


async def _confirmed(
    rt: Runtime, tier: PlanTier = PlanTier.ENTERPRISE, company: str = "Acme Corp"
) -> Deal:
    n = next(_counter)
    async with rt.db.session() as session:
        deal = Deal(
            gmail_message_id=f"m{n}",
            gmail_thread_id=f"t{n}",
            sender_email="ravi@x.com",
            customer_company=company,
            contact_email="jane@acme.com",
        )
        session.add(deal)
        await session.flush()
        for status in (DealStatus.AWAITING_TIER, DealStatus.CALLING):
            deal = await transition(session, deal, status, actor="t", rationale="setup")
        return await transition(
            session,
            deal,
            DealStatus.TIER_CONFIRMED,
            actor="t",
            rationale="setup",
            changes={"plan_tier": tier},
        )


async def _deal(rt: Runtime, deal_id: str) -> Deal:
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
    assert deal is not None
    return deal


# --- happy path and template accuracy ----------------------------------------------


@pytest.mark.parametrize(
    ("tier", "template", "days", "csm"),
    [(PlanTier.ENTERPRISE, 111, 30, "anita@x.com"), (PlanTier.GROWTH, 222, 14, "pod@x.com")],
)
async def test_project_is_created_from_the_tiers_template(
    rt: Runtime, projects: FakeProjectStore, tier: PlanTier, template: int, days: int, csm: str
) -> None:
    deal = await _confirmed(rt, tier)

    await create_project_for(rt, deal.id)

    done = await _deal(rt, deal.id)
    assert done.status is DealStatus.PROJECT_CREATED
    [request] = projects.created
    assert request.template_id == template
    assert TEMPLATES.for_tier(tier).duration_days == days
    assert request.csm_email == csm
    assert request.owner_email == "priya@x.com"
    assert done.rocketlane_project_id == next(iter(projects.projects))
    assert done.csm_email == csm


async def test_dedicated_csms_rotate_across_deals(rt: Runtime, projects: FakeProjectStore) -> None:
    for company in ("Acme", "Globex", "Initech"):
        deal = await _confirmed(rt, company=company)
        await create_project_for(rt, deal.id)
    assert [p.csm_email for p in projects.created] == ["anita@x.com", "vikram@x.com", "anita@x.com"]


async def test_decision_is_audited(rt: Runtime) -> None:
    deal = await _confirmed(rt)
    await create_project_for(rt, deal.id)
    async with rt.db.session() as session:
        rows = (await session.exec(select(AuditLog).where(AuditLog.deal_id == deal.id))).all()
    created = next(r for r in rows if r.action == "create_project")
    assert created.status == "ok"
    assert created.rationale == "created from template 111 for the enterprise tier"
    assert any(r.action == "update_fields" for r in rows)  # the CSM choice


# --- Rocketlane down ---------------------------------------------------------------


async def test_rocketlane_down_retries_then_escalates(
    rt: Runtime, projects: FakeProjectStore, jobs: FakeJobScheduler
) -> None:
    deal = await _confirmed(rt)
    projects.fail("create_project", *[RetryableError("rocketlane 503")] * 4)

    for attempt, delay in ((1, 60), (2, 300), (3, 900)):
        await create_project_for(rt, deal.id, attempt)
        assert (await _deal(rt, deal.id)).status is DealStatus.RL_RETRY_WAIT
        job = jobs.pop(f"project:{deal.id}:{attempt + 1}")
        assert job.args == (deal.id, str(attempt + 1))
        assert abs((job.run_at - (await _deal(rt, deal.id)).updated_at).total_seconds() - delay) < 5

    await create_project_for(rt, deal.id, 4)

    done = await _deal(rt, deal.id)
    assert done.status is DealStatus.ESCALATED
    assert done.rocketlane_project_id is None
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]


async def test_csm_is_kept_across_retries(rt: Runtime, projects: FakeProjectStore) -> None:
    deal = await _confirmed(rt)
    projects.fail("create_project", RetryableError("503"))
    await create_project_for(rt, deal.id, 1)
    await create_project_for(rt, deal.id, 2)
    assert [p.csm_email for p in projects.created] == ["anita@x.com"]
    assert (await _deal(rt, deal.id)).status is DealStatus.PROJECT_CREATED


async def test_listing_failure_is_retried_too(
    rt: Runtime, projects: FakeProjectStore, jobs: FakeJobScheduler
) -> None:
    deal = await _confirmed(rt)
    projects.fail("list_projects", RetryableError("timeout"))
    await create_project_for(rt, deal.id, 1)
    assert (await _deal(rt, deal.id)).status is DealStatus.RL_RETRY_WAIT
    assert f"project:{deal.id}:2" in jobs.jobs


# --- a create that timed out but succeeded -----------------------------------------


async def test_timed_out_create_is_adopted_not_duplicated(
    rt: Runtime, projects: FakeProjectStore, jobs: FakeJobScheduler
) -> None:
    deal = await _confirmed(rt)
    projects.lose_next_response(OutcomeUnknownError("read timeout"))

    await create_project_for(rt, deal.id, 1)  # created upstream; we only saw a timeout
    assert (await _deal(rt, deal.id)).status is DealStatus.RL_RETRY_WAIT
    jobs.pop(f"project:{deal.id}:2")

    await create_project_for(rt, deal.id, 2)

    done = await _deal(rt, deal.id)
    assert done.status is DealStatus.PROJECT_CREATED
    assert done.rocketlane_project_id == next(iter(projects.projects))
    assert len(projects.projects) == 1  # never a second project
    assert len(projects.called("create_project")) == 1  # the retry adopted, didn't create


# --- existing project for the customer -----------------------------------------------


async def test_existing_project_holds_and_escalates(
    rt: Runtime, projects: FakeProjectStore, jobs: FakeJobScheduler
) -> None:
    projects.seed(
        ProjectRef(project_id="900", name="ACME onboarding", customer_company="ACME Corporation")
    )
    deal = await _confirmed(rt)

    await create_project_for(rt, deal.id)

    assert (await _deal(rt, deal.id)).status is DealStatus.ESCALATED
    assert not projects.created
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]
    async with rt.db.session() as session:
        rows = (await session.exec(select(AuditLog).where(AuditLog.deal_id == deal.id))).all()
    path = [(r.input_json or {}).get("to") for r in rows if r.action == "transition"]
    assert path[-2:] == ["duplicate_hold", "escalated"]
    assert any("ACME onboarding (id 900)" in (r.rationale or "") for r in rows)


async def test_archived_project_for_the_customer_is_not_a_duplicate(
    rt: Runtime, projects: FakeProjectStore
) -> None:
    projects.seed(
        ProjectRef(project_id="1", name="old", customer_company="Acme Corp", is_active=False)
    )
    deal = await _confirmed(rt)
    await create_project_for(rt, deal.id)
    assert (await _deal(rt, deal.id)).status is DealStatus.PROJECT_CREATED


# --- permanent failures ------------------------------------------------------------


async def test_rejected_request_escalates_at_once(
    rt: Runtime, projects: FakeProjectStore, jobs: FakeJobScheduler
) -> None:
    deal = await _confirmed(rt)
    projects.fail("create_project", PermanentError("rocketlane: HTTP 400: Owner not found"))

    await create_project_for(rt, deal.id)

    assert (await _deal(rt, deal.id)).status is DealStatus.ESCALATED
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]  # no retries for a 4xx


async def test_missing_template_escalates(rt: Runtime, projects: FakeProjectStore) -> None:
    no_template = TEMPLATES.model_copy(
        update={"growth": TEMPLATES.growth.model_copy(update={"rocketlane_template_id": None})}
    )
    configured = replace(rt, templates=no_template)
    deal = await _confirmed(configured, PlanTier.GROWTH)

    await create_project_for(configured, deal.id)

    assert (await _deal(rt, deal.id)).status is DealStatus.ESCALATED
    assert not projects.created


async def test_rocketlane_not_configured_escalates(rt: Runtime) -> None:
    bare = replace(rt, projects=None)
    deal = await _confirmed(bare)
    await create_project_for(bare, deal.id)
    assert (await _deal(bare, deal.id)).status is DealStatus.ESCALATED


# --- idempotency and entry points --------------------------------------------------


async def test_running_twice_creates_once(rt: Runtime, projects: FakeProjectStore) -> None:
    deal = await _confirmed(rt)
    await create_project_for(rt, deal.id)
    await create_project_for(rt, deal.id)
    assert len(projects.created) == 1


async def test_human_tier_confirmation_queues_the_project(
    rt: Runtime, jobs: FakeJobScheduler
) -> None:
    async with rt.db.session() as session:
        deal = Deal(
            gmail_message_id="mh",
            gmail_thread_id="th",
            sender_email="r@x.com",
            customer_company="Acme",
        )
        session.add(deal)
        await session.flush()
        deal = await transition(session, deal, DealStatus.AWAITING_TIER, actor="t", rationale="t")
        deal = await escalate(session, deal, actor="t", reason="tier unclear")

    _, queued = await resolve(
        rt,
        deal.id,
        Resolution(
            action=ResolveAction.CONFIRM_TIER,
            actor="Priya",
            reason="Confirmed on Slack",
            plan_tier=PlanTier.GROWTH,
        ),
    )

    assert queued == "project_queued"
    assert f"project:{deal.id}:1" in jobs.jobs


async def test_advance_does_not_double_queue(rt: Runtime, jobs: FakeJobScheduler) -> None:
    deal = await _confirmed(rt)
    assert await advance(rt, deal) == "project_queued"
    assert await advance(rt, deal) == "already_queued"
