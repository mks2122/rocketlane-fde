"""Entry points for scheduled jobs. APScheduler stores these as "module:function" strings.

Each job takes only strings (picklable, and stable across deploys) and fetches the
process Runtime when it fires.

Every job is `tracked`, so shutdown can wait for jobs in flight (`drain`) before it
closes the database. Otherwise a job cut off mid-transaction leaves a task that never
finishes, and the shutdown hangs.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from app.orchestrator import escalation, intake, project, sweep, voice
from app.orchestrator.runtime import get_runtime

_in_flight: set[asyncio.Task[Any]] = set()


def tracked[**P](fn: Callable[P, Awaitable[Any]]) -> Callable[P, Coroutine[Any, Any, None]]:
    @functools.wraps(fn)
    async def run(*args: P.args, **kwargs: P.kwargs) -> None:
        task = asyncio.current_task()
        if task is not None:
            _in_flight.add(task)
        try:
            await fn(*args, **kwargs)
        finally:
            if task is not None:
                _in_flight.discard(task)

    return run


async def drain(timeout_seconds: float) -> int:
    """Wait for running jobs to finish. Returns how many were still running at the timeout."""
    running = {task for task in _in_flight if not task.done()}
    if not running:
        return 0
    _, still_running = await asyncio.wait(running, timeout=timeout_seconds)
    return len(still_running)


@tracked
async def start_call_job(deal_id: str) -> None:
    await voice.start_call_attempt(get_runtime(), deal_id)


@tracked
async def process_call_job(call_attempt_id: str) -> None:
    await voice.process_call_result(get_runtime(), int(call_attempt_id))


@tracked
async def escalation_notice_job(deal_id: str, attempt: str) -> None:
    await escalation.send_escalation_notice(get_runtime(), deal_id, int(attempt))


@tracked
async def sweep_job() -> None:
    await sweep.sweep(get_runtime())


@tracked
async def intake_job(deal_id: str, attempt: str) -> None:
    await intake.process_intake(get_runtime(), deal_id, int(attempt))


@tracked
async def clarification_job(deal_id: str, stage: str, version: str) -> None:
    await intake.clarification_followup(get_runtime(), deal_id, stage, int(version))


@tracked
async def project_job(deal_id: str, attempt: str) -> None:
    await project.create_project_for(get_runtime(), deal_id, int(attempt))
