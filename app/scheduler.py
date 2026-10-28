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

from datetime import UTC

from apscheduler.jobstores.memory import MemoryJobStore  # pyright: ignore[reportMissingTypeStubs]
from apscheduler.jobstores.sqlalchemy import (  # pyright: ignore[reportMissingTypeStubs]
    SQLAlchemyJobStore,
)
from apscheduler.schedulers.asyncio import (  # pyright: ignore[reportMissingTypeStubs]
    AsyncIOScheduler,
)

from app.db import Database

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
