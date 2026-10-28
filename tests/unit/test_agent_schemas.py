"""Agent output models must compile to OpenAI's strict JSON schema. Offline, no tokens."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agents import AgentOutputSchema
from pydantic import BaseModel

from app.agents.intake import thread_text
from app.agents.runner import load_prompt
from app.ports import DealIntake, InboundEmail, TierVerdict


@pytest.mark.parametrize("model", [DealIntake, TierVerdict])
def test_output_schema_is_strict_compatible(model: type[BaseModel]) -> None:
    schema = AgentOutputSchema(model, strict_json_schema=True).json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize("name", ["intake", "tier_verifier"])
def test_prompts_exist_and_mention_untrusted_data(name: str) -> None:
    assert "UNTRUSTED" in load_prompt(name)


def test_thread_text_leaves_out_our_own_messages() -> None:
    at = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
    ae = InboundEmail(
        message_id="m1",
        thread_id="t",
        from_email="ravi@x.com",
        from_name="Ravi",
        subject="Deal: Acme",
        body_text="Customer: Acme",
        received_at=at,
    )
    ours = ae.model_copy(
        update={
            "message_id": "m2",
            "from_email": "cs@x.com",
            "body_text": "Contact email?",
            "from_self": True,
        }
    )
    text = thread_text([ae, ours])
    assert "Customer: Acme" in text
    assert "Contact email?" not in text
    assert "From: Ravi <ravi@x.com>" in text
