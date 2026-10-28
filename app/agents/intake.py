"""Intake Agent: reads a deal email thread into a `DealIntake` (values + verbatim evidence).

No tools, no side effects. Validation and grounding live in `services/intake_validation.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from app.agents.runner import StructuredAgent
from app.agents.untrusted import wrap_untrusted
from app.config import Settings
from app.ports import DealIntake, InboundEmail

log = structlog.get_logger(__name__)


def thread_text(emails: Sequence[InboundEmail]) -> str:
    """The AE's side of the thread as plain text, oldest first.

    Our own messages are left out: intake must only ever read what the AE wrote.
    """
    parts = [
        f"From: {e.from_name or ''} <{e.from_email}>\n"
        f"Date: {e.received_at.isoformat()}\n"
        f"Subject: {e.subject}\n\n{e.body_text}"
        for e in emails
        if not e.from_self
    ]
    return "\n\n-----\n\n".join(parts)


class AgentIntakeExtractor:
    def __init__(self, settings: Settings) -> None:
        self._agent = StructuredAgent(
            settings, name="intake_agent", prompt="intake", output_type=DealIntake
        )

    async def extract(self, *, thread_text: str) -> DealIntake:
        intake = await self._agent.run(wrap_untrusted("DEAL EMAIL THREAD", thread_text))
        log.info("intake_extracted", is_deal_handoff=intake.is_deal_handoff)
        return intake
