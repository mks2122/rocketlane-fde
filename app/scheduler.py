"""APScheduler setup.

Two job stores:
- "default" (SQLite via the sync engine) holds one-off jobs that must survive a restart,
  such as call retries and Rocketlane retries (P4/P7).
- "memory" holds recurring jobs that are re-registered at every startup, like the inbox
  poll. Their targets are bound methods, which can't be pickled into the database.

Job defaults: `coalesce` folds missed runs into one, and `max_instances=1` means a slow
poll is never overlapped by the next one.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

import structlog
from apscheduler.jobstores.memory import MemoryJobStore  # pyright: ignore[reportMissingTypeStubs]
from apscheduler.jobstores.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
    SQLAlchemyJobStore,
)
from apscheduler.schedulers.asyncio import (  # pyright: ignore[reportMissingTypeStubs]
    AsyncIOScheduler,
)

from app.db import Database

log = structlog.get_logger(__name__)

MEMORY_STORE = "memory"


def create_scheduler(db: Database) -> AsyncIOScheduler:
    return AsyncIOScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(engine=db.sync_engine),
            MEMORY_STORE: MemoryJobStore(),
        },
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
        timezone=UTC,
    )


class APSchedulerJobs:
    """`JobScheduler` on APScheduler's SQLite job store: jobs survive a restart.

    Jobs are referenced as "module:function" strings, so nothing unpicklable is stored.
    `misfire_grace_time=None` runs a job that came due while the app was down as soon as
    it is back, instead of silently dropping it.
    """

    def __init__(self, scheduler: AsyncIOScheduler) -> None:
        self._scheduler = scheduler

    def schedule(self, job: str, *, run_at: datetime, args: Sequence[str], job_id: str) -> None:
        self._scheduler.add_job(  # pyright: ignore[reportUnknownMemberType]
            job,
            "date",
            run_date=run_at,
            args=list(args),
            id=job_id,
            replace_existing=True,
            jobstore="default",
            misfire_grace_time=None,
        )
        log.info("job_scheduled", job=job, job_id=job_id, run_at=run_at.isoformat())

    def has_pending(self, job_id_prefix: str) -> bool:
        # APScheduler ships no type information; job ids are the strings we assigned.
        jobs = cast(list[Any], self._scheduler.get_jobs(jobstore="default"))  # pyright: ignore[reportUnknownMemberType]
        return any(str(job.id).startswith(job_id_prefix) for job in jobs)
