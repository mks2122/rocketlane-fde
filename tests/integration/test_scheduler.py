"""The real APScheduler adapter on the SQLite job store."""

from __future__ import annotations

from datetime import timedelta

from app.db import Database
from app.models import utcnow
from app.scheduler import APSchedulerJobs, create_scheduler

JOB = "app.orchestrator.jobs:start_call_job"


async def test_jobs_persist_and_can_be_found_by_prefix(db: Database) -> None:
    scheduler = create_scheduler(db)
    scheduler.start(paused=True)  # store jobs without running them
    try:
        jobs = APSchedulerJobs(scheduler)
        later = utcnow() + timedelta(hours=1)
        jobs.schedule(JOB, run_at=later, args=["d_1"], job_id="call:d_1:1")
        jobs.schedule(JOB, run_at=later, args=["d_1"], job_id="call:d_1:1")  # replaces

        assert jobs.has_pending("call:d_1:")
        assert not jobs.has_pending("call:d_2:")
        stored = scheduler.get_jobs(jobstore="default")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        assert len(stored) == 1  # pyright: ignore[reportUnknownArgumentType]
    finally:
        scheduler.shutdown(wait=False)

    # A fresh scheduler on the same database still sees the job: it survives a restart.
    restarted = create_scheduler(db)
    restarted.start(paused=True)
    try:
        assert APSchedulerJobs(restarted).has_pending("call:d_1:")
    finally:
        restarted.shutdown(wait=False)
