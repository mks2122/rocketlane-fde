from __future__ import annotations

from dataclasses import replace

import pytest

from app.domain import PlanTier
from app.services.slack_copy import (
    CHANNEL_PATTERN,
    ChannelCopy,
    ChannelFacts,
    channel_name,
    fallback_copy,
    problems_with,
)

FACTS = ChannelFacts(
    customer_company="Globex Corp",
    tier=PlanTier.ENTERPRISE,
    duration_days=30,
    csm_name="Paul",
    csm_email="paul@x.com",
    owner_email="rachel@x.com",
    contact_name="Hank Scorpio",
    project_url="https://acme.rocketlane.com/projects/1",
)
GOOD = ChannelCopy(
    topic="Globex Corp · Enterprise onboarding",
    welcome_message="Welcome Globex Corp! You're on Enterprise; Paul is your dedicated CSM.",
)


@pytest.mark.parametrize(
    ("company", "deal_id", "expected"),
    [
        ("Globex Corp", "d_1a2b3c4d5e6f", "onb-globex-corp-5e6f"),
        ("  Café & Co. ", "d_abcd", "onb-caf-co-abcd"),
        ("!!!", "d_9999", "onb-customer-9999"),
    ],
)
def test_channel_names(company: str, deal_id: str, expected: str) -> None:
    assert channel_name(company, deal_id) == expected


def test_channel_name_is_always_valid_and_bounded() -> None:
    name = channel_name("A" * 300, "d_ffff")
    assert len(name) <= 80
    assert CHANNEL_PATTERN.match(name)
    assert name.endswith("-ffff")


def test_channel_name_differs_per_deal() -> None:
    assert channel_name("Globex", "d_aaaa1111") != channel_name("Globex", "d_bbbb2222")


def test_good_copy_passes() -> None:
    assert problems_with(GOOD, FACTS) == []


@pytest.mark.parametrize(
    ("copy", "problem"),
    [
        (ChannelCopy(topic="", welcome_message=GOOD.welcome_message), "topic"),
        (ChannelCopy(topic=GOOD.topic, welcome_message="Hi Paul, Enterprise!"), "customer name"),
        (ChannelCopy(topic=GOOD.topic, welcome_message="Globex Corp Enterprise"), "CSM"),
        (ChannelCopy(topic=GOOD.topic, welcome_message="Globex Corp Paul Growth"), "plan tier"),
        (
            ChannelCopy(topic=GOOD.topic, welcome_message=GOOD.welcome_message + " <!channel>"),
            "@channel",
        ),
        (
            ChannelCopy(topic=GOOD.topic, welcome_message=GOOD.welcome_message + " @here"),
            "@channel",
        ),
        (ChannelCopy(topic=GOOD.topic, welcome_message="x" * 3001), "message must be"),
    ],
)
def test_bad_copy_is_rejected(copy: ChannelCopy, problem: str) -> None:
    assert any(problem in p for p in problems_with(copy, FACTS))


@pytest.mark.parametrize("tier", list(PlanTier))
def test_fallback_always_passes_validation(tier: PlanTier) -> None:
    facts = replace(FACTS, tier=tier)
    copy = fallback_copy(facts)
    assert problems_with(copy, facts) == []


def test_fallback_is_tier_specific() -> None:
    enterprise = fallback_copy(FACTS).welcome_message
    growth = fallback_copy(replace(FACTS, tier=PlanTier.GROWTH)).welcome_message
    assert "dedicated Customer Success Manager" in enterprise
    assert "onboarding pod" in growth
    assert FACTS.project_url is not None
    assert FACTS.project_url in enterprise
