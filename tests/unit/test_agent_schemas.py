"""Agent output models must compile to OpenAI's strict JSON schema. Offline, no tokens."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from agents import AgentOutputSchema
from pydantic import BaseModel

from app.agents.comms import build_input
from app.agents.intake import thread_text
from app.agents.runner import load_prompt
from app.domain import PlanTier
from app.ports import ChannelCopy, ChannelFacts, DealIntake, InboundEmail, TierVerdict


@pytest.mark.parametrize("model", [DealIntake, TierVerdict, ChannelCopy])
def test_output_schema_is_strict_compatible(model: type[BaseModel]) -> None:
    schema = AgentOutputSchema(model, strict_json_schema=True).json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


@pytest.mark.parametrize("name", ["intake", "tier_verifier"])
def test_prompts_exist_and_mention_untrusted_data(name: str) -> None:
    assert "UNTRUSTED" in load_prompt(name)


def test_comms_prompt_forbids_broadcasts_and_invention() -> None:
    prompt = load_prompt("comms")
    assert "@channel" in prompt
    assert "never invent" in prompt


def test_comms_input_carries_feedback() -> None:
    facts = ChannelFacts(
        customer_company="Globex",
        tier=PlanTier.GROWTH,
        duration_days=14,
        csm_name="Paul",
        csm_email="paul@x.com",
        owner_email="r@x.com",
    )
    text = build_input(facts, ["message must mention the CSM (Paul)"])
    assert '"customer_company": "Globex"' in text
    assert "- message must mention the CSM (Paul)" in text
    assert "rejected" not in build_input(facts, [])


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
