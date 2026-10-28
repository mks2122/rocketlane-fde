"""Tier Verifier: an LLM's independent reading of the call transcript (the second key, G5).

It has no tools and no side effects. It returns a typed `TierVerdict`, and the two-key
rule in `services/call_policy.py` decides what that means.
"""

from __future__ import annotations

import structlog

from app.agents.runner import StructuredAgent
from app.agents.untrusted import wrap_untrusted
from app.config import Settings
from app.ports import TierVerdict

log = structlog.get_logger(__name__)


def build_input(*, transcript: str, customer_company: str) -> str:
    return (
        f"Customer company (from the deal email): {customer_company}\n\n"
        f"{wrap_untrusted('CALL TRANSCRIPT', transcript)}"
    )


class AgentTierVerifier:
    def __init__(self, settings: Settings) -> None:
        self._agent = StructuredAgent(
            settings, name="tier_verifier", prompt="tier_verifier", output_type=TierVerdict
        )

    async def verify(self, *, transcript: str, customer_company: str) -> TierVerdict:
        verdict = await self._agent.run(
            build_input(transcript=transcript, customer_company=customer_company)
        )
        log.info("tier_verified", tier=verdict.tier, ae_confirmed=verdict.ae_confirmed)
        return verdict
