"""Project routing: which template, owner and CSM a confirmed deal gets. Pure, apart from
the round-robin cursor (`next_csm_index`), which is one atomic SQL statement.

Template accuracy (the brief's "templates get mixed up" pain point) is structural: the
template comes from a fixed tier → config mapping, never from free text or an LLM.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, timedelta
from typing import Final

from sqlalchemy import text
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config import TemplatesConfig, TierConfig
from app.domain import PlanTier
from app.errors import ConfigError
from app.models import Deal
from app.ports import NewProject, ProjectRef

# Legal-form suffixes ignored when comparing company names for duplicates.
_SUFFIXES: Final = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "llc",
        "llp",
        "plc",
        "gmbh",
        "ag",
        "sa",
        "bv",
        "pvt",
        "private",
        "pte",
        "the",
    }
)
_NON_WORD = re.compile(r"[^\w]+")


def normalise_company(name: str) -> str:
    """'ACME Corp.', 'Acme Corporation' and 'The Acme, Inc.' all become 'acme'."""
    words = [w for w in _NON_WORD.sub(" ", name.casefold()).split() if w not in _SUFFIXES]
    return " ".join(words)


def find_own_project(projects: Sequence[ProjectRef], deal_id: str) -> ProjectRef | None:
    """The project this deal already created (e.g. a create that timed out but succeeded)."""
    return next((p for p in projects if p.deal_marker == deal_id), None)


def find_duplicates(
    projects: Sequence[ProjectRef], *, customer_company: str, deal_id: str
) -> list[ProjectRef]:
    """Active projects for the same customer that this deal did not create."""
    target = normalise_company(customer_company)
    if not target:
        return []
    return [
        p
        for p in projects
        if p.is_active
        and p.deal_marker != deal_id
        and p.customer_company is not None
        and normalise_company(p.customer_company) == target
    ]


def pick_csm(tier: TierConfig, cursor: int) -> str:
    """Dedicated: round-robin over the pool. Pooled: the one shared pod user."""
    if tier.csm_mode == "pooled":
        return tier.csm_pool[0]
    return tier.csm_pool[cursor % len(tier.csm_pool)]


async def next_csm_index(session: AsyncSession, tier: PlanTier) -> int:
    """Claim the next round-robin slot for `tier` atomically (no two deals get the same slot).

    One UPDATE ... RETURNING instead of read-then-write, so concurrent deals can't read
    the same cursor. Works on SQLite (3.35+) and Postgres.
    """
    conn = await session.connection()
    params = {"tier": tier.name}
    await conn.execute(
        text("INSERT INTO routing_state (tier, cursor) VALUES (:tier, 0) ON CONFLICT DO NOTHING"),
        params,
    )
    result = await conn.execute(
        text("UPDATE routing_state SET cursor = cursor + 1 WHERE tier = :tier RETURNING cursor"),
        params,
    )
    return int(result.scalar_one()) - 1


def build_project(
    deal: Deal, *, tier: PlanTier, config: TemplatesConfig, csm_email: str, start: date
) -> NewProject:
    tier_config = config.for_tier(tier)
    if tier_config.rocketlane_template_id is None:
        raise ConfigError(f"no Rocketlane template configured for the {tier} tier")
    if not deal.customer_company:
        raise ConfigError("deal has no customer company")  # intake guarantees it; defensive
    project = config.project
    fields: list[tuple[int, str]] = []
    if project.plan_tier_field_id is not None:
        fields.append((project.plan_tier_field_id, tier.value.title()))
    if project.sf_opportunity_field_id is not None and deal.sf_opportunity_url:
        fields.append((project.sf_opportunity_field_id, deal.sf_opportunity_url))
    return NewProject(
        deal_id=deal.id,
        project_name=project.name_format.format(
            customer=deal.customer_company, tier=tier.value.title()
        ),
        customer_company=deal.customer_company,
        plan_tier=tier,
        template_id=tier_config.rocketlane_template_id,
        start_date=start,
        due_date=start + timedelta(days=tier_config.duration_days),
        owner_email=tier_config.project_owner,
        csm_email=csm_email,
        csm_placeholder_id=tier_config.csm_placeholder_id,
        customer_contact_email=deal.contact_email if project.invite_customer_contact else None,
        assign_unassigned_tasks_to_owner=project.assign_unassigned_tasks_to_owner,
        custom_fields=tuple(fields),
    )
