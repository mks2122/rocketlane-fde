"""Check templates.yaml against a Rocketlane workspace. Pure: takes what the API returned.

Used by `scripts/check_rocketlane.py` so config mistakes (a wrong template id, an owner
who isn't a workspace user) show up before a deal hits them.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.config import TemplatesConfig
from app.domain import PlanTier


def check_config(
    config: TemplatesConfig,
    *,
    templates: Iterable[Mapping[str, Any]],
    users: Iterable[Mapping[str, Any]],
) -> list[str]:
    """Human-readable problems; empty when the config matches the workspace."""
    problems: list[str] = []
    by_id = {int(t["templateId"]): t for t in templates}
    members = {
        str(u.get("email", "")).lower()
        for u in users
        if u.get("type") == "TEAM_MEMBER" and u.get("status") == "ACTIVE"
    }

    ids: dict[PlanTier, int] = {}
    for tier in PlanTier:
        tier_config = config.for_tier(tier)
        template_id = tier_config.rocketlane_template_id
        if template_id is None:
            problems.append(f"{tier}: rocketlane_template_id is not set")
        elif template_id not in by_id:
            problems.append(f"{tier}: template {template_id} does not exist in the workspace")
        elif by_id[template_id].get("objectType") != "PROJECT":
            problems.append(f"{tier}: template {template_id} is not a PROJECT template")
        else:
            ids[tier] = template_id
        people = [("project_owner", tier_config.project_owner)]
        people += [("csm_pool", email) for email in tier_config.csm_pool]
        for role, email in people:
            if email.lower() not in members:
                problems.append(f"{tier}: {role} {email} is not an active team member")

    if len(ids) == 2 and ids[PlanTier.ENTERPRISE] == ids[PlanTier.GROWTH]:
        problems.append("enterprise and growth use the same template: templates would be mixed up")
    return problems
