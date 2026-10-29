"""Recovery sweep: stranded deals get their work queued again, nothing else is touched."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.asyncio import (  # pyright: ignore[reportMissingTypeStubs]
    AsyncIOScheduler,
)
from fastapi.testclient import TestClient
from sqlmodel import select

from app.config import Settings
from app.db import Database
from app.domain import CallOutcome, DealStatus, PlanTier
from app.integrations.fakes import FakeJobScheduler, FakeTierVerifier, FakeVoiceCaller
from app.main import create_app
from app.models import AuditLog, CallAttempt, Deal
from app.orchestrator import voice as flow
from app.orchestrator.escalation import escalate
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.orchestrator.sweep import sweep

WED_10AM = datetime(2026, 10, 28, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)


@pytest.fixture
def jobs() -> FakeJobScheduler:
    return FakeJobScheduler()


@pytest.fixture
def now(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    current = [WED_10AM]
    monkeypatch.setattr(flow, "utcnow", lambda: current[0])
    monkeypatch.setattr("app.orchestrator.sweep.utcnow", lambda: current[0])
    return current


@pytest.fixture
def rt(settings: Settings, db: Database, jobs: FakeJobScheduler, now: list[datetime]) -> Runtime:
    return Runtime(
        settings=settings,
        db=db,
        voice=FakeVoiceCaller(),
        tier_verifier=FakeTierVerifier(),
        jobs=jobs,
    )


async def _deal(rt: Runtime, *path: DealStatus, **fields: Any) -> Deal:
    async with rt.db.session() as session:
        deal = Deal(
            gmail_message_id=f"m-{len(path)}-{id(path)}",
            gmail_thread_id=f"t-{id(path)}",
            sender_email="ravi@x.com",
            customer_company="Acme Corp",
            ae_name="Ravi",
            ae_phone="+919876543210",
            **fields,
        )
        session.add(deal)
        await session.flush()
        for status in path:
            if status is DealStatus.ESCALATED:
                deal = await escalate(session, deal, actor="t", reason="setup")
            else:
                deal = await transition(session, deal, status, actor="t", rationale="setup")
        return deal


async def _calling_deal(rt: Runtime, *, started: datetime, ended: bool) -> tuple[Deal, int]:
    deal = await _deal(rt, DealStatus.AWAITING_TIER, DealStatus.CALLING)
    async with rt.db.session() as session:
        attempt = CallAttempt(
            deal_id=deal.id,
            attempt_no=1,
            vapi_call_id="call_1",
            started_at=started,
            ended_at=started + timedelta(minutes=2) if ended else None,
            ended_reason="customer-ended-call" if ended else None,
        )
        session.add(attempt)
        await session.flush()
        assert attempt.id is not None
        return deal, attempt.id


async def _status(rt: Runtime, deal_id: str) -> DealStatus:
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
    assert deal is not None
    return deal.status


async def test_waiting_deal_without_a_job_gets_its_call_queued(
    rt: Runtime, jobs: FakeJobScheduler
) -> None:
    deal = await _deal(rt, DealStatus.AWAITING_TIER)

    report = await sweep(rt)
    again = await sweep(rt)

    assert report["call_queued"] == 1
    assert again["already_queued"] == 1
    assert list(jobs.jobs) == [f"call:{deal.id}:1"]


async def test_owed_escalation_notice_is_queued(rt: Runtime, jobs: FakeJobScheduler) -> None:
    deal = await _deal(rt, DealStatus.ESCALATED)
    await sweep(rt)
    assert f"escalation-notice:{deal.id}:1" in jobs.jobs


async def test_notified_escalation_is_left_alone(rt: Runtime, jobs: FakeJobScheduler) -> None:
    deal = await _deal(rt, DealStatus.ESCALATED)
    async with rt.db.session() as session:
        stored = await session.get(Deal, deal.id)
        assert stored is not None
        stored.escalation_notified_at = datetime.now(UTC)

    report = await sweep(rt)

    assert report == {"nothing_to_do": 1}
    assert not jobs.jobs


async def test_unprocessed_end_of_call_report_is_queued(
    rt: Runtime, jobs: FakeJobScheduler, now: list[datetime]
) -> None:
    _, attempt_id = await _calling_deal(rt, started=now[0] - timedelta(minutes=5), ended=True)

    report = await sweep(rt)

    assert report == {"call_processing_queued": 1}
    assert f"process-call:{attempt_id}" in jobs.jobs


async def test_call_that_never_reported_is_failed_and_retried(
    rt: Runtime, jobs: FakeJobScheduler, now: list[datetime]
) -> None:
    deal, attempt_id = await _calling_deal(rt, started=now[0] - timedelta(hours=1), ended=False)

    report = await sweep(rt)

    assert report == {"call_timed_out": 1}
    assert await _status(rt, deal.id) is DealStatus.CALL_RETRY_WAIT
    async with rt.db.session() as session:
        attempt = await session.get(CallAttempt, attempt_id)
    assert attempt is not None
    assert attempt.outcome == CallOutcome.CALL_ERROR
    assert attempt.outcome_reason is not None
    assert "no end-of-call report" in attempt.outcome_reason
    assert f"call:{deal.id}:2" in jobs.jobs


async def test_live_call_is_not_touched(
    rt: Runtime, jobs: FakeJobScheduler, now: list[datetime]
) -> None:
    deal, _ = await _calling_deal(rt, started=now[0] - timedelta(minutes=1), ended=False)

    report = await sweep(rt)

    assert report == {"call_in_progress": 1}
    assert await _status(rt, deal.id) is DealStatus.CALLING
    assert not jobs.jobs


async def test_terminal_and_later_phase_deals_are_not_touched(
    rt: Runtime, jobs: FakeJobScheduler
) -> None:
    await _deal(rt, DealStatus.IGNORED)
    await _deal(rt, DealStatus.AWAITING_TIER, DealStatus.CALLING)  # no attempt row: inconsistent
    jobs.jobs.clear()

    report = await sweep(rt)

    assert report == {"inconsistent": 1}
    assert not jobs.jobs


async def _tier_confirmed(rt: Runtime) -> Deal:
    deal = await _deal(rt, DealStatus.AWAITING_TIER, DealStatus.CALLING)
    async with rt.db.session() as session:
        return await transition(
            session,
            deal,
            DealStatus.TIER_CONFIRMED,
            actor="t",
            rationale="t",
            changes={"plan_tier": PlanTier.GROWTH},
        )


async def test_tier_confirmed_gets_project_creation_queued(
    rt: Runtime, jobs: FakeJobScheduler
) -> None:
    deal = await _tier_confirmed(rt)

    report = await sweep(rt)

    assert report == {"project_queued": 1}
    assert list(jobs.jobs) == [f"project:{deal.id}:1"]


async def test_stranded_retry_wait_resumes_at_the_right_attempt(
    rt: Runtime, jobs: FakeJobScheduler
) -> None:
    deal = await _tier_confirmed(rt)
    async with rt.db.session() as session:
        for _ in range(2):  # two failed attempts so far
            deal = await transition(
                session, deal, DealStatus.RL_RETRY_WAIT, actor="t", rationale="t"
            )
            deal = await transition(
                session, deal, DealStatus.TIER_CONFIRMED, actor="t", rationale="t"
            )
        deal = await transition(session, deal, DealStatus.RL_RETRY_WAIT, actor="t", rationale="t")

    await sweep(rt)

    assert list(jobs.jobs) == [f"project:{deal.id}:4"]


async def test_project_created_gets_its_channel_queued(rt: Runtime, jobs: FakeJobScheduler) -> None:
    deal = await _tier_confirmed(rt)
    async with rt.db.session() as session:
        await transition(session, deal, DealStatus.PROJECT_CREATED, actor="t", rationale="t")

    report = await sweep(rt)

    assert report == {"channel_queued": 1}
    assert list(jobs.jobs) == [f"channel:{deal.id}:1"]


async def test_received_deal_gets_intake_queued(rt: Runtime, jobs: FakeJobScheduler) -> None:
    deal = await _deal(rt)  # RECEIVED

    report = await sweep(rt)

    assert report == {"intake_queued": 1}
    assert list(jobs.jobs) == [f"intake:{deal.id}:1"]


async def test_needs_info_deal_gets_its_timers_back(rt: Runtime, jobs: FakeJobScheduler) -> None:
    deal = await _deal(rt, DealStatus.NEEDS_INFO)

    report = await sweep(rt)

    assert report == {"followups_queued": 1}
    assert sorted(jobs.jobs) == [
        f"clarify:{deal.id}:{deal.version}:escalate",
        f"clarify:{deal.id}:{deal.version}:reminder",
    ]


async def test_recoveries_are_audited(rt: Runtime) -> None:
    deal = await _deal(rt, DealStatus.AWAITING_TIER)
    await sweep(rt)
    await sweep(rt)  # nothing new to do: no second audit row

    async with rt.db.session() as session:
        rows = (
            await session.exec(
                select(AuditLog).where(AuditLog.deal_id == deal.id, AuditLog.action == "recover")
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].actor == "sweeper"
    assert rows[0].output_json == {"queued": "call_queued"}


@pytest.mark.parametrize("enabled", [True, False])
def test_sweep_is_scheduled_at_startup(
    settings: Settings, jobs: FakeJobScheduler, enabled: bool
) -> None:
    configured = settings.model_copy(update={"sweep_enabled": enabled})
    app = create_app(
        configured, voice=FakeVoiceCaller(), tier_verifier=FakeTierVerifier(), jobs=jobs
    )
    with TestClient(app):
        scheduler: AsyncIOScheduler = app.state.scheduler
        job = scheduler.get_job("recovery_sweep")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    assert (job is not None) is enabled
