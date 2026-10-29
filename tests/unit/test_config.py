from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.config import (
    AEDirectory,
    Settings,
    TemplatesConfig,
    load_ae_directory,
    load_templates,
)
from app.domain import PlanTier
from app.errors import ConfigError
from tests.conftest import REPO_ROOT

TIER = {
    "duration_days": 30,
    "csm_mode": "dedicated",
    "csm_pool": ["a@x.com", "b@x.com"],
    "project_owner": "o@x.com",
}
POOLED = {**TIER, "duration_days": 14, "csm_mode": "pooled", "csm_pool": ["pod@x.com"]}


def _settings(**overrides: object) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        admin_token=SecretStr("x" * 16),
        **overrides,  # pyright: ignore[reportArgumentType]
    )


def test_repo_templates_file_is_valid() -> None:
    templates = load_templates(REPO_ROOT / "config" / "templates.yaml")
    assert templates.for_tier(PlanTier.ENTERPRISE).duration_days == 30
    assert templates.for_tier(PlanTier.ENTERPRISE).csm_mode == "dedicated"
    assert templates.for_tier(PlanTier.GROWTH).duration_days == 14
    assert templates.for_tier(PlanTier.GROWTH).csm_mode == "pooled"


def test_enterprise_must_be_dedicated() -> None:
    with pytest.raises(ValidationError, match="enterprise tier must use a dedicated CSM"):
        TemplatesConfig.model_validate({"enterprise": POOLED, "growth": POOLED})


def test_growth_must_be_pooled() -> None:
    with pytest.raises(ValidationError, match="growth tier must use a pooled CSM"):
        TemplatesConfig.model_validate({"enterprise": TIER, "growth": TIER})


def test_pooled_tier_needs_exactly_one_user() -> None:
    with pytest.raises(ValidationError, match="exactly one shared CSM"):
        TemplatesConfig.model_validate(
            {"enterprise": TIER, "growth": {**POOLED, "csm_pool": ["a@x.com", "b@x.com"]}}
        )


def test_unknown_template_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        TemplatesConfig.model_validate({"enterprise": {**TIER, "typo": 1}, "growth": POOLED})


def test_ae_lookup_is_case_insensitive(ae_directory_file: Path) -> None:
    directory = load_ae_directory(ae_directory_file)
    found = directory.lookup("  RAVI.AE@example.com ")
    assert found is not None
    assert found.phone == "+919876543210"
    assert directory.lookup("unknown@example.com") is None


def test_ae_phone_must_be_e164() -> None:
    with pytest.raises(ValidationError):
        AEDirectory.model_validate(
            {"account_executives": [{"name": "R", "email": "r@x.com", "phone": "98765 43210"}]}
        )


def test_duplicate_ae_emails_rejected() -> None:
    ae = {"name": "R", "email": "r@x.com", "phone": "+919876543210"}
    with pytest.raises(ValidationError, match="duplicate AE email"):
        AEDirectory.model_validate({"account_executives": [ae, {**ae, "email": "R@X.com"}]})


def test_missing_config_file_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="config file not found"):
        load_templates(tmp_path / "nope.yaml")


def test_admin_token_is_required_and_long() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, admin_token=SecretStr("short"))  # pyright: ignore[reportCallIssue]


def test_retry_delays_must_cover_attempts() -> None:
    with pytest.raises(ValidationError, match="one delay per retry"):
        _settings(call_max_attempts=3, call_retry_delays_seconds=[60])


def test_invalid_timezone_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown timezone"):
        _settings(call_timezone="Mars/Olympus")


def test_require_names_missing_settings() -> None:
    settings = _settings()
    with pytest.raises(ConfigError, match="VAPI_API_KEY, ROCKETLANE_API_KEY"):
        settings.require("vapi_api_key", "rocketlane_api_key")


def test_secrets_are_masked_in_repr() -> None:
    settings = _settings(openai_api_key=SecretStr("sk-very-secret"))
    assert "sk-very-secret" not in repr(settings)


def test_blank_env_values_count_as_missing(tmp_path: Path) -> None:
    # Regression: `VAPI_API_KEY=` used to load as SecretStr("") and pass require().
    env_file = tmp_path / ".env"
    env_file.write_text(
        "ADMIN_TOKEN=xxxxxxxxxxxxxxxx\nVAPI_API_KEY=\nTWILIO_PHONE_NUMBER=\n", encoding="utf-8"
    )
    settings = Settings(_env_file=env_file)  # pyright: ignore[reportCallIssue]
    assert settings.vapi_api_key is None
    assert settings.twilio_phone_number is None
    with pytest.raises(ConfigError, match="VAPI_API_KEY"):
        settings.require("vapi_api_key")


def test_backoff_max_must_not_be_below_base() -> None:
    with pytest.raises(ValidationError, match="http_backoff_max_seconds"):
        _settings(http_backoff_base_seconds=5, http_backoff_max_seconds=1)


def test_project_defaults_and_name_format() -> None:
    config = TemplatesConfig.model_validate({"enterprise": TIER, "growth": POOLED})
    assert config.project.name_format.format(customer="Acme", tier="Enterprise") == (
        "Acme - Enterprise onboarding"
    )
    assert config.project.invite_customer_contact is False


def test_unknown_name_placeholder_is_rejected() -> None:
    with pytest.raises(ValidationError, match="name_format"):
        TemplatesConfig.model_validate(
            {"enterprise": TIER, "growth": POOLED, "project": {"name_format": "{deal_id}"}}
        )


@pytest.mark.parametrize(
    "field",
    [
        "call_retry_delays_seconds",
        "rocketlane_retry_delays_seconds",
        "escalation_notice_retry_delays_seconds",
    ],
)
def test_retry_delays_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError, match="retry delays must be positive"):
        _settings(**{field: [60, 0]})


def test_display_names() -> None:
    config = TemplatesConfig.model_validate(
        {"enterprise": TIER, "growth": POOLED, "team": {"A@x.com": "Anita"}}
    )
    assert config.display_name("a@X.com") == "Anita"
    assert config.display_name("vikram.csm@x.com") == "Vikram Csm"


@pytest.mark.parametrize(("raw", "expected"), [("0.3", 0.3), ("null", None)])
def test_openai_temperature_can_be_left_out(
    tmp_path: Path, raw: str, expected: float | None
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"ADMIN_TOKEN=xxxxxxxxxxxxxxxx\nOPENAI_TEMPERATURE={raw}\n", encoding="utf-8"
    )
    assert Settings(_env_file=env_file).openai_temperature == expected  # pyright: ignore[reportCallIssue]


def test_openai_temperature_defaults_to_zero(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("ADMIN_TOKEN=xxxxxxxxxxxxxxxx\nOPENAI_TEMPERATURE=\n", encoding="utf-8")
    assert Settings(_env_file=env_file).openai_temperature == 0.0  # pyright: ignore[reportCallIssue]
