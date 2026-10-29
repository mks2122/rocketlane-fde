from __future__ import annotations

from typing import Any

from app.config import TemplatesConfig
from app.services.rocketlane_check import check_config

TEMPLATES = [
    {"templateId": 111, "templateName": "Enterprise", "objectType": "PROJECT"},
    {"templateId": 222, "templateName": "Growth", "objectType": "PROJECT"},
    {"templateId": 333, "templateName": "Kickoff notes", "objectType": "DOCUMENT"},
]
USERS = [
    {"email": "Priya@x.com", "type": "TEAM_MEMBER", "status": "ACTIVE"},
    {"email": "anita@x.com", "type": "TEAM_MEMBER", "status": "ACTIVE"},
    {"email": "pod@x.com", "type": "TEAM_MEMBER", "status": "ACTIVE"},
    {"email": "gone@x.com", "type": "TEAM_MEMBER", "status": "INACTIVE"},
    {"email": "cust@acme.com", "type": "CUSTOMER", "status": "ACTIVE"},
]


def _config(ent: int | None = 111, gro: int | None = 222, **ent_extra: Any) -> TemplatesConfig:
    return TemplatesConfig.model_validate(
        {
            "enterprise": {
                "rocketlane_template_id": ent,
                "duration_days": 30,
                "csm_mode": "dedicated",
                "csm_pool": ["anita@x.com"],
                "project_owner": "priya@x.com",
                **ent_extra,
            },
            "growth": {
                "rocketlane_template_id": gro,
                "duration_days": 14,
                "csm_mode": "pooled",
                "csm_pool": ["pod@x.com"],
                "project_owner": "priya@x.com",
            },
        }
    )


def test_matching_config_has_no_problems() -> None:
    assert check_config(_config(), templates=TEMPLATES, users=USERS) == []


def test_template_problems() -> None:
    problems = check_config(_config(ent=None, gro=333), templates=TEMPLATES, users=USERS)
    assert problems == [
        "enterprise: rocketlane_template_id is not set",
        "growth: template 333 is not a PROJECT template",
    ]
    assert check_config(_config(gro=999), templates=TEMPLATES, users=USERS) == [
        "growth: template 999 does not exist in the workspace"
    ]


def test_same_template_for_both_tiers_is_flagged() -> None:
    problems = check_config(_config(ent=111, gro=111), templates=TEMPLATES, users=USERS)
    assert problems == ["enterprise and growth use the same template: templates would be mixed up"]


def test_users_must_be_active_team_members() -> None:
    config = _config(csm_pool=["gone@x.com", "cust@acme.com"])
    problems = check_config(config, templates=TEMPLATES, users=USERS)
    assert problems == [
        "enterprise: csm_pool gone@x.com is not an active team member",
        "enterprise: csm_pool cust@acme.com is not an active team member",
    ]
