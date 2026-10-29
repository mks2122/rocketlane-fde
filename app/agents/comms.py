"""Communication Agent: writes the onboarding channel's topic and welcome message.

It sees only validated deal facts (no email text) and has no tools. Code validates the
draft (`services/slack_copy.problems_with`) and owns the channel name and every Slack call.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog

from app.agents.runner import StructuredAgent
from app.config import Settings
from app.ports import ChannelCopy, ChannelFacts

log = structlog.get_logger(__name__)


def build_input(facts: ChannelFacts, feedback: Sequence[str]) -> str:
    text = f"Facts:\n{facts.model_dump_json(indent=2)}"
    if feedback:
        text += "\n\nYour previous draft was rejected. Fix these points:\n" + "\n".join(
            f"- {point}" for point in feedback
        )
    return text


class AgentCommsWriter:
    def __init__(self, settings: Settings) -> None:
        self._agent = StructuredAgent(
            settings, name="comms_agent", prompt="comms", output_type=ChannelCopy
        )

    async def write(self, facts: ChannelFacts, *, feedback: Sequence[str] = ()) -> ChannelCopy:
        copy = await self._agent.run(build_input(facts, feedback))
        log.info("comms_drafted", chars=len(copy.welcome_message), retry=bool(feedback))
        return copy
