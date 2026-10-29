"""Run a structured-output agent with domain error mapping and bounded retries.

Every agent in this system is a single-turn, tool-less call that must return its
`output_type`. This helper is the one place that maps OpenAI and Agents SDK failures
onto domain errors:

- connection errors, rate limits and 5xx → RetryableError (retried here, bounded);
- 4xx, a missing key, invalid structured output, a wrong output type → PermanentError.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import openai
from agents import Agent, AgentsException, ModelSettings, Runner
from pydantic import BaseModel

from app.config import Settings
from app.errors import PermanentError, RetryableError
from app.integrations.retry import RetryPolicy, run_with_retries

PROMPTS_DIR = Path(__file__).parent / "prompts"


@cache
def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


class StructuredAgent[T: BaseModel]:
    def __init__(self, settings: Settings, *, name: str, prompt: str, output_type: type[T]) -> None:
        self.name = name
        self._output_type = output_type
        self._agent = Agent(
            name=name,
            instructions=load_prompt(prompt),
            output_type=output_type,
            model=settings.openai_model,
            model_settings=ModelSettings(temperature=settings.openai_temperature),
        )
        self._policy = RetryPolicy.from_settings(settings)

    async def run(self, user_input: str) -> T:
        async def once() -> T:
            try:
                result = await Runner.run(self._agent, user_input, max_turns=1)
                return result.final_output_as(self._output_type, raise_if_incorrect_type=True)
            except (openai.APIConnectionError, openai.RateLimitError) as exc:
                raise RetryableError(f"{self.name}: {type(exc).__name__}") from exc
            except openai.APIStatusError as exc:
                # Classify by status, not exception class: a 429 can arrive as a plain
                # APIStatusError, and it's still a "slow down", not a failure.
                if exc.status_code == 429 or exc.status_code >= 500:
                    raise RetryableError(f"{self.name}: HTTP {exc.status_code}") from exc
                # OpenAI's message says what to fix (bad parameter, unknown model); it
                # holds no secrets and ends up in the escalation notice for a human.
                raise PermanentError(
                    f"{self.name}: HTTP {exc.status_code}: {exc.message[:300]}"
                ) from exc
            except (openai.OpenAIError, AgentsException, TypeError) as exc:
                raise PermanentError(f"{self.name}: {type(exc).__name__}: {exc}") from exc

        return await run_with_retries(once, service=self.name, policy=self._policy)
