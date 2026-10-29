"""Onboarding channel copy: naming, validation, and the fixed fallback. Pure.

The Comms Agent writes the topic and welcome message. Code owns everything that must be
exact: the channel name (deterministic, so retries find the same channel), the facts the
message has to contain, and a fixed template used when the agent fails or its draft fails
validation twice. Slack is never blocked on the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from app.domain import PlanTier

MAX_CHANNEL_CHARS: Final = 80
MAX_TOPIC_CHARS: Final = 250  # Slack's topic limit
MAX_MESSAGE_CHARS: Final = 3000
CHANNEL_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9_-]{0,79}$")
# Broadcast mentions would ping a whole workspace or channel.
_BROADCASTS: Final = re.compile(r"<!(channel|here|everyone)>|@(channel|here|everyone)\b", re.I)
_NON_SLUG = re.compile(r"[^a-z0-9]+")

TIER_LABEL: Final = {PlanTier.ENTERPRISE: "Enterprise", PlanTier.GROWTH: "Growth"}


@dataclass(frozen=True)
class ChannelFacts:
    """Everything the copy may state. All values come from validated deal data."""

    customer_company: str
    tier: PlanTier
    duration_days: int
    csm_name: str
    csm_email: str
    owner_email: str
    contact_name: str | None
    project_url: str | None


@dataclass(frozen=True)
class ChannelCopy:
    topic: str
    welcome_message: str


def channel_name(customer_company: str, deal_id: str) -> str:
    """`onb-<customer>-<4 chars of the deal id>`: readable, unique per deal, stable on retry."""
    slug = _NON_SLUG.sub("-", customer_company.casefold()).strip("-") or "customer"
    suffix = re.sub(r"[^a-z0-9]", "", deal_id.casefold())[-4:] or "0000"
    room = MAX_CHANNEL_CHARS - len("onb--") - len(suffix)
    return f"onb-{slug[:room].rstrip('-')}-{suffix}"


def problems_with(copy: ChannelCopy, facts: ChannelFacts) -> list[str]:
    """Reasons a draft can't be posted; empty when it's fine."""
    problems: list[str] = []
    text = copy.welcome_message
    if not copy.topic.strip() or len(copy.topic) > MAX_TOPIC_CHARS:
        problems.append(f"topic must be 1-{MAX_TOPIC_CHARS} characters")
    if not text.strip() or len(text) > MAX_MESSAGE_CHARS:
        problems.append(f"message must be 1-{MAX_MESSAGE_CHARS} characters")
    lowered = text.casefold()
    for label, value in (
        ("customer name", facts.customer_company),
        ("plan tier", TIER_LABEL[facts.tier]),
        ("CSM", facts.csm_name),
    ):
        if value.casefold() not in lowered:
            problems.append(f"message must mention the {label} ({value})")
    if _BROADCASTS.search(text) or _BROADCASTS.search(copy.topic):
        problems.append("no @channel / @here / @everyone")
    return problems


def fallback_copy(facts: ChannelFacts) -> ChannelCopy:
    tier = TIER_LABEL[facts.tier]
    support = (
        f"{facts.csm_name} is your dedicated Customer Success Manager"
        if facts.tier is PlanTier.ENTERPRISE
        else f"{facts.csm_name} from our onboarding pod is looking after you"
    )
    lines = [
        f"Welcome to {facts.customer_company}'s NovaCRM onboarding! 🎉",
        f"Plan: {tier}. Target: live in {facts.duration_days} working days.",
        f"{support} ({facts.csm_email}).",
    ]
    if facts.contact_name:
        lines.append(f"Customer contact: {facts.contact_name}.")
    if facts.project_url:
        lines.append(f"Project plan: {facts.project_url}")
    lines.append("Next step: we'll schedule the kickoff call. Questions? Ask here anytime.")
    return ChannelCopy(
        topic=f"{facts.customer_company} · {tier} onboarding · CSM {facts.csm_name}"[
            :MAX_TOPIC_CHARS
        ],
        welcome_message="\n".join(lines),
    )
