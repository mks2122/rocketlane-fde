"""Job tracking used for graceful shutdown."""

from __future__ import annotations

import asyncio

from app.orchestrator.jobs import drain, tracked


async def test_drain_waits_for_running_jobs() -> None:
    finished: list[str] = []

    @tracked
    async def slow() -> None:
        await asyncio.sleep(0.05)
        finished.append("slow")

    task = asyncio.create_task(slow())
    await asyncio.sleep(0)  # let it start
    assert await drain(timeout_seconds=2) == 0
    assert finished == ["slow"]
    assert task.done()


async def test_drain_reports_jobs_still_running_at_timeout() -> None:
    release = asyncio.Event()

    @tracked
    async def stuck() -> None:
        await release.wait()

    task = asyncio.create_task(stuck())
    await asyncio.sleep(0)
    assert await drain(timeout_seconds=0.01) == 1
    release.set()
    await task
    assert await drain(timeout_seconds=0.01) == 0


async def test_failed_jobs_are_untracked() -> None:
    @tracked
    async def boom() -> None:
        raise RuntimeError("bug")

    task = asyncio.create_task(boom())
    await asyncio.gather(task, return_exceptions=True)
    assert await drain(timeout_seconds=0.01) == 0


async def test_drain_with_nothing_running() -> None:
    assert await drain(timeout_seconds=0) == 0
