"""Application settings (.env) and business configuration (YAML).

Integration credentials are optional at load time so that each phase can run
without every account in place; adapters call `require()` when they start, which
fails fast with the exact variable name that is missing.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain import PlanTier
from app.errors import ConfigError

E164_PATTERN = r"^\+[1-9]\d{7,14}$"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = False

    # Async SQLAlchemy URL. Postgres in prod: postgresql+asyncpg://...
    database_url: str = "sqlite+aiosqlite:///./data/novacrm.db"

    admin_token: SecretStr = Field(min_length=16)

    # Business config
    templates_path: Path = Path("config/templates.yaml")
    ae_directory_path: Path = Path("config/ae_directory.yaml")

    # OpenAI / Agents SDK
    openai_api_key: SecretStr | None = None
    openai_model: str = "gpt-4.1-mini"
    trace_include_sensitive_data: bool = True

    # Gmail
    gmail_credentials_path: Path = Path("secrets/credentials.json")
    gmail_token_path: Path = Path("secrets/token.json")
    gmail_poll_seconds: int = Field(default=30, ge=5, le=3600)
    allowed_sender_domains: list[str] = Field(default_factory=list)

    # Vapi
    vapi_api_key: SecretStr | None = None
    vapi_assistant_id: str | None = None
    vapi_phone_number_id: str | None = None
    vapi_webhook_secret: SecretStr | None = None
    public_base_url: str | None = None

    # Call policy
    call_max_attempts: int = Field(default=3, ge=1, le=10)
    call_retry_delays_seconds: list[int] = Field(default_factory=lambda: [600, 1800])
    call_window_start: time = time(9, 0)
    call_window_end: time = time(19, 0)
    call_timezone: str = "Asia/Kolkata"

    # Rocketlane
    rocketlane_api_key: SecretStr | None = None
    rocketlane_base_url: str = "https://api.rocketlane.com/api/1.0"

    # Slack
    slack_bot_token: SecretStr | None = None
    slack_escalation_channel: str = "cs-escalations"

    # Escalation
    escalation_email: EmailStr | None = None

    # HTTP
    http_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    http_read_timeout_seconds: float = Field(default=20.0, gt=0)

    @field_validator("call_retry_delays_seconds")
    @classmethod
    def _positive_delays(cls, v: list[int]) -> list[int]:
        if any(d <= 0 for d in v):
            raise ValueError("call retry delays must be positive")
        return v

    @field_validator("call_timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {v}") from exc
        return v

    @model_validator(mode="after")
    def _retry_delays_cover_attempts(self) -> Self:
        if len(self.call_retry_delays_seconds) < self.call_max_attempts - 1:
            raise ValueError("call_retry_delays_seconds needs one delay per retry attempt")
        if self.call_window_start >= self.call_window_end:
            raise ValueError("call_window_start must be before call_window_end")
        return self

    def require(self, *names: str) -> None:
        """Fail fast with the env var names of any missing integration settings."""
        missing = [n.upper() for n in names if getattr(self, n) in (None, "")]
        if missing:
            raise ConfigError(f"missing required settings: {', '.join(missing)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # populated from env


# --- Business configuration -------------------------------------------------


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TierConfig(_Frozen):
    rocketlane_template_id: int | None = None  # filled once the template exists in Rocketlane
    duration_days: PositiveInt
    csm_mode: Literal["dedicated", "pooled"]
    csm_pool: list[EmailStr] = Field(min_length=1)
    project_owner: EmailStr

    @model_validator(mode="after")
    def _pooled_has_one_assignee(self) -> Self:
        if self.csm_mode == "pooled" and len(self.csm_pool) != 1:
            raise ValueError("pooled tier must have exactly one shared CSM user")
        if len(set(self.csm_pool)) != len(self.csm_pool):
            raise ValueError("csm_pool contains duplicates")
        return self


class TemplatesConfig(_Frozen):
    enterprise: TierConfig
    growth: TierConfig

    @model_validator(mode="after")
    def _tier_rules(self) -> Self:
        # Encodes the customer's contract: Enterprise = dedicated CSM, Growth = pooled CSM.
        if self.enterprise.csm_mode != "dedicated":
            raise ValueError("enterprise tier must use a dedicated CSM")
        if self.growth.csm_mode != "pooled":
            raise ValueError("growth tier must use a pooled CSM")
        return self

    def for_tier(self, tier: PlanTier) -> TierConfig:
        return self.enterprise if tier is PlanTier.ENTERPRISE else self.growth


class AccountExecutive(_Frozen):
    name: str = Field(min_length=1)
    email: EmailStr
    phone: str = Field(pattern=E164_PATTERN)


class AEDirectory(_Frozen):
    account_executives: list[AccountExecutive]

    @model_validator(mode="after")
    def _unique_emails(self) -> Self:
        emails = [ae.email.lower() for ae in self.account_executives]
        if len(set(emails)) != len(emails):
            raise ValueError("duplicate AE email in directory")
        return self

    def lookup(self, email: str) -> AccountExecutive | None:
        needle = email.strip().lower()
        return next((ae for ae in self.account_executives if ae.email.lower() == needle), None)


def _load_yaml(path: Path) -> object:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_templates(path: Path) -> TemplatesConfig:
    return TemplatesConfig.model_validate(_load_yaml(path))


def load_ae_directory(path: Path) -> AEDirectory:
    return AEDirectory.model_validate(_load_yaml(path))
