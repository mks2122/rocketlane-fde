"""Error mapping of the shared agent runner. Runner.run is faked: no model is called."""

from __future__ import annotations

from typing import Any

import httpx2  # the HTTP client package openai is built on
import openai
import pytest
from agents import ModelBehaviorError, Runner

from app.agents.runner import StructuredAgent
from app.config import Settings
from app.errors import PermanentError, RetryableError
from app.ports import TierVerdict

REQUEST = httpx2.Request("POST", "https://api.openai.test/v1/responses")


def _status(code: int) -> openai.APIStatusError:
    return openai.APIStatusError("boom", response=httpx2.Response(code, request=REQUEST), body=None)


@pytest.fixture
def agent(settings: Settings) -> StructuredAgent[TierVerdict]:
    fast = settings.model_copy(
        update={
            "http_max_attempts": 3,
            "http_backoff_base_seconds": 0,
            "http_backoff_max_seconds": 0,
        }
    )
    return StructuredAgent(
        fast, name="tier_verifier", prompt="tier_verifier", output_type=TierVerdict
    )


def _fake_run(monkeypatch: pytest.MonkeyPatch, *outcomes: Exception | object) -> list[int]:
    calls: list[int] = []
    queue = list(outcomes)

    async def run(*_args: Any, **_kwargs: Any) -> object:
        calls.append(1)
        outcome = queue.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(Runner, "run", run)
    return calls


class _Result:
    def __init__(self, output: object) -> None:
        self.final_output = output

    def final_output_as(self, cls: type[Any], raise_if_incorrect_type: bool = False) -> Any:
        if raise_if_incorrect_type and not isinstance(self.final_output, cls):
            raise TypeError("wrong output type")
        return self.final_output


VERDICT = TierVerdict(tier="growth", ae_confirmed=True, evidence_quote="Growth", rationale="r")  # pyright: ignore[reportArgumentType]


async def test_returns_typed_output(
    agent: StructuredAgent[TierVerdict], monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_run(monkeypatch, _Result(VERDICT))
    assert await agent.run("x") == VERDICT


@pytest.mark.parametrize(
    "error",
    [openai.APIConnectionError(request=REQUEST), _status(429), _status(503)],
    ids=["connection", "rate-limit", "5xx"],
)
async def test_transient_errors_are_retried(
    agent: StructuredAgent[TierVerdict], monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    calls = _fake_run(monkeypatch, error, _Result(VERDICT))
    assert await agent.run("x") == VERDICT
    assert len(calls) == 2


async def test_transient_errors_are_bounded(
    agent: StructuredAgent[TierVerdict], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _fake_run(monkeypatch, *[_status(502)] * 3)
    with pytest.raises(RetryableError):
        await agent.run("x")
    assert len(calls) == 3


@pytest.mark.parametrize(
    "outcome",
    [_status(401), ModelBehaviorError("invalid JSON"), _Result("not a verdict")],
    ids=["4xx", "invalid-output", "wrong-type"],
)
async def test_permanent_errors_are_not_retried(
    agent: StructuredAgent[TierVerdict], monkeypatch: pytest.MonkeyPatch, outcome: object
) -> None:
    calls = _fake_run(monkeypatch, outcome)
    with pytest.raises(PermanentError):
        await agent.run("x")
    assert len(calls) == 1
