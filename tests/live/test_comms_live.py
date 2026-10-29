"""Real-model check of the Comms Agent. Skipped by default; run with `uv run pytest -m live`."""

from __future__ import annotations

import os

import pytest
from agents import set_default_openai_key

from app.agents.comms import AgentCommsWriter
from app.config import Settings
from app.domain import PlanTier
from app.ports import ChannelFacts
from app.services.slack_copy import problems_with

pytestmark = pytest.mark.live


@pytest.mark.parametrize("tier", list(PlanTier))
async def test_draft_passes_validation(settings: Settings, tier: PlanTier) -> None:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        pytest.skip("OPENAI_API_KEY not set")
    set_default_openai_key(key)
    facts = ChannelFacts(
        customer_company="Globex Corp",
        tier=tier,
        duration_days=30 if tier is PlanTier.ENTERPRISE else 14,
        csm_name="Paul",
        csm_email="paul@novacrm-demo.com",
        owner_email="rachel@novacrm-demo.com",
        contact_name="Hank Scorpio",
        project_url="https://example.rocketlane.com/projects/1",
    )
    copy = await AgentCommsWriter(settings).write(facts)
    assert problems_with(copy, facts) == []
