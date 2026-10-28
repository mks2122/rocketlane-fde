"""Routing: template accuracy, CSM assignment, duplicate detection, project request."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from app.config import TemplatesConfig
from app.db import Database
from app.domain import PlanTier
from app.errors import ConfigError
from app.models import Deal
from app.ports import ProjectRef
from app.services.routing import (
    build_project,
    find_duplicates,
    find_own_project,
    next_csm_index,
    normalise_company,
    pick_csm,
)

CONFIG = TemplatesConfig.model_validate(
    {
        "enterprise": {
            "rocketlane_template_id": 111,
            "duration_days": 30,
            "csm_mode": "dedicated",
            "csm_pool": ["anita@x.com", "vikram@x.com"],
            "project_owner": "priya@x.com",
            "csm_placeholder_id": 77,
        },
        "growth": {
            "rocketlane_template_id": 222,
            "duration_days": 14,
            "csm_mode": "pooled",
            "csm_pool": ["pod@x.com"],
            "project_owner": "priya@x.com",
        },
        "project": {"plan_tier_field_id": 9, "sf_opportunity_field_id": 10},
    }
)
START = date(2026, 10, 28)


def _deal(**fields: Any) -> Deal:
    values: dict[str, Any] = {
        "id": "d_1",
        "gmail_message_id": "m",
        "gmail_thread_id": "t",
        "sender_email": "ravi@x.com",
        "customer_company": "Acme Corp",
        "contact_email": "jane@acme.com",
        "sf_opportunity_url": "https://x.lightning.force.com/006",
        **fields,
    }
    return Deal(**values)


# --- template accuracy (Part 4) ----------------------------------------------------


@pytest.mark.parametrize(
    ("tier", "template", "days", "owner"),
    [(PlanTier.ENTERPRISE, 111, 30, "priya@x.com"), (PlanTier.GROWTH, 222, 14, "priya@x.com")],
)
def test_tier_routing(tier: PlanTier, template: int, days: int, owner: str) -> None:
    project = build_project(_deal(), tier=tier, config=CONFIG, csm_email="anita@x.com", start=START)
    assert project.template_id == template
    assert (project.due_date - project.start_date).days == days
    assert project.owner_email == owner
    assert project.plan_tier is tier


def test_the_repo_config_never_mixes_templates() -> None:
    from app.config import load_templates
    from tests.conftest import REPO_ROOT

    repo = load_templates(REPO_ROOT / "config" / "templates.yaml")
    ent, gro = repo.for_tier(PlanTier.ENTERPRISE), repo.for_tier(PlanTier.GROWTH)
    assert (ent.duration_days, ent.csm_mode) == (30, "dedicated")
    assert (gro.duration_days, gro.csm_mode) == (14, "pooled")
    if ent.rocketlane_template_id is not None and gro.rocketlane_template_id is not None:
        assert ent.rocketlane_template_id != gro.rocketlane_template_id


def test_project_request_details() -> None:
    project = build_project(
        _deal(), tier=PlanTier.ENTERPRISE, config=CONFIG, csm_email="vikram@x.com", start=START
    )
    assert project.project_name == "Acme Corp - Enterprise onboarding"
    assert project.deal_id == "d_1"
    assert project.csm_email == "vikram@x.com"
    assert project.csm_placeholder_id == 77
    assert project.custom_fields == ((9, "Enterprise"), (10, "https://x.lightning.force.com/006"))
    assert project.customer_contact_email is None  # invites are off by default


def test_customer_invite_when_enabled() -> None:
    inviting = CONFIG.model_copy(
        update={"project": CONFIG.project.model_copy(update={"invite_customer_contact": True})}
    )
    project = build_project(
        _deal(), tier=PlanTier.GROWTH, config=inviting, csm_email="pod@x.com", start=START
    )
    assert project.customer_contact_email == "jane@acme.com"


def test_missing_template_is_a_config_error() -> None:
    no_template = CONFIG.model_copy(
        update={"growth": CONFIG.growth.model_copy(update={"rocketlane_template_id": None})}
    )
    with pytest.raises(ConfigError, match="no Rocketlane template configured for the growth tier"):
        build_project(
            _deal(), tier=PlanTier.GROWTH, config=no_template, csm_email="pod@x.com", start=START
        )


# --- CSM assignment ----------------------------------------------------------------


def test_dedicated_round_robin_and_pooled_pod() -> None:
    assert [pick_csm(CONFIG.enterprise, i) for i in range(4)] == [
        "anita@x.com",
        "vikram@x.com",
        "anita@x.com",
        "vikram@x.com",
    ]
    assert {pick_csm(CONFIG.growth, i) for i in range(4)} == {"pod@x.com"}


async def test_cursor_claims_are_atomic_under_concurrency(db: Database) -> None:
    async def claim() -> int:
        async with db.session() as session:
            return await next_csm_index(session, PlanTier.ENTERPRISE)

    claims = await asyncio.gather(*(claim() for _ in range(8)))
    assert sorted(claims) == list(range(8))  # every slot handed out exactly once

    async with db.session() as session:
        assert await next_csm_index(session, PlanTier.GROWTH) == 0  # per-tier cursor


# --- duplicates and our own marker -------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Acme Corp", "ACME Corporation"),
        ("Acme, Inc.", "acme"),
        ("The Acme Company Pvt. Ltd.", "Acme"),
        ("Globex GmbH", "globex"),
    ],
)
def test_company_names_normalise_together(a: str, b: str) -> None:
    assert normalise_company(a) == normalise_company(b)


def test_different_companies_stay_different() -> None:
    assert normalise_company("Acme Labs") != normalise_company("Acme")


PROJECTS = [
    ProjectRef(project_id="1", name="ACME onboarding", customer_company="ACME Corporation"),
    ProjectRef(project_id="2", name="old", customer_company="Acme", is_active=False),
    ProjectRef(project_id="3", name="ours", customer_company="Acme Corp", deal_marker="d_1"),
    ProjectRef(project_id="4", name="other", customer_company="Globex"),
]


def test_duplicates_are_active_same_customer_not_ours() -> None:
    dupes = find_duplicates(PROJECTS, customer_company="Acme Corp", deal_id="d_1")
    assert [p.project_id for p in dupes] == ["1"]


def test_own_project_is_found_by_marker() -> None:
    own = find_own_project(PROJECTS, "d_1")
    assert own is not None
    assert own.project_id == "3"
    assert find_own_project(PROJECTS, "d_2") is None
