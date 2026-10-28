"""Tier Verifier: an LLM's independent reading of the call transcript (the second key, G5).

It has no tools and no side effects. It returns a typed `TierVerdict`, and the two-key
rule in `services/call_policy.py` decides what that means.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Final

import openai
import structlog
from agents import Agent, AgentsException, ModelSettings, Runner

from app.agents.untrusted import wrap_untrusted
from app.config import Settings
from app.errors import PermanentError, RetryableError
from app.integrations.retry import RetryPolicy, run_with_retries
from app.ports import TierVerdict

log = structlog.get_logger(__name__)

PROMPT_PATH: Final = Path(__file__).parent / "prompts" / "tier_verifier.md"
SERVICE: Final = "tier_verifier"


@cache
def _instructions() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def build_input(*, transcript: str, customer_company: str) -> str:
    return (
        f"Customer company (from the deal email): {customer_company}\n\n"
        f"{wrap_untrusted('CALL TRANSCRIPT', transcript)}"
    )


class AgentTierVerifier:
    def __init__(self, settings: Settings) -> None:
        self._agent = Agent(
            name="tier_verifier",
            instructions=_instructions(),
            output_type=TierVerdict,
            model=settings.openai_model,
            model_settings=ModelSettings(temperature=0),
        )
        self._policy = RetryPolicy.from_settings(settings)

    async def verify(self, *, transcript: str, customer_company: str) -> TierVerdict:
        prompt = build_input(transcript=transcript, customer_company=customer_company)

        async def once() -> TierVerdict:
            try:
                result = await Runner.run(self._agent, prompt, max_turns=1)
                return result.final_output_as(TierVerdict, raise_if_incorrect_type=True)
            except (openai.APIConnectionError, openai.RateLimitError) as exc:
                raise RetryableError(f"{SERVICE}: {type(exc).__name__}") from exc
            except openai.APIStatusError as exc:
                if exc.status_code >= 500:
                    raise RetryableError(f"{SERVICE}: HTTP {exc.status_code}") from exc
                raise PermanentError(f"{SERVICE}: HTTP {exc.status_code}") from exc
            except (openai.OpenAIError, AgentsException, TypeError) as exc:
                # Invalid structured output, refused schema, missing key, wrong output type.
                raise PermanentError(f"{SERVICE}: {type(exc).__name__}: {exc}") from exc

        verdict = await run_with_retries(once, service=SERVICE, policy=self._policy)
        log.info("tier_verified", tier=verdict.tier, ae_confirmed=verdict.ae_confirmed)
        return verdict
