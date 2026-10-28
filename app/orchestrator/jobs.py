"""Entry points for scheduled jobs. APScheduler stores these as "module:function" strings.

Each job takes only strings (picklable, and stable across deploys) and fetches the
process Runtime when it fires.
"""

from __future__ import annotations

from app.orchestrator import voice
from app.orchestrator.runtime import get_runtime


async def start_call_job(deal_id: str) -> None:
    await voice.start_call_attempt(get_runtime(), deal_id)


async def process_call_job(call_attempt_id: str) -> None:
    await voice.process_call_result(get_runtime(), int(call_attempt_id))
