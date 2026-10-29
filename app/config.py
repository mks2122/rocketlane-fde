"""Application settings (.env) and business configuration (YAML).

Integration credentials are optional at load time so that each phase can run
without every account in place; adapters call `require()` when they start, which
fails fast with the exact variable name that is missing.
"""

from __future__ import annotations

import re
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

from app.domain import INTAKE_FIELDS, PlanTier
from app.errors import ConfigError

E164_PATTERN = r"^\+[1-9]\d{7,14}$"


class Settings(BaseSettings):
    # env_ignore_empty: `VAPI_API_KEY=` (blank, as in .env.example) means "not configured",
    # so it falls back to None instead of becoming SecretStr("") and slipping past require().
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True
    )

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
    gmail_enabled: bool = False  # off until scripts/gmail_auth.py has produced a token
    gmail_credentials_path: Path = Path("secrets/credentials.json")
    gmail_token_path: Path = Path("secrets/token.json")
    gmail_poll_seconds: int = Field(default=30, ge=5, le=3600)
    gmail_max_poll_backoff_seconds: int = Field(default=300, ge=5)
    gmail_processed_label: str = Field(default="novacrm-processed", pattern=r"^[a-z0-9-]+$")
    # Only look this far back, so connecting an old inbox doesn't replay its history.
    gmail_lookback_days: int = Field(default=7, ge=1, le=90)
    gmail_max_body_chars: int = Field(default=50_000, ge=1_000)
    # Intake (Q1 pending: the required set is config, not code)
    intake_required_fields: list[str] = Field(default_factory=lambda: list(INTAKE_FIELDS))
    # Clarification rounds with the AE before a human takes over.
    intake_max_clarifications: int = Field(default=2, ge=1, le=5)
    clarification_reminder_seconds: int = Field(default=86_400, ge=60)  # demo: 120
    clarification_escalate_seconds: int = Field(default=172_800, ge=120)  # demo: 240

    # Sender allowlist (G6). Fail closed: with both lists empty, every email is ignored.
    allowed_sender_domains: list[str] = Field(default_factory=list)
    allowed_sender_emails: list[EmailStr] = Field(default_factory=list)

    # Vapi
    vapi_base_url: str = "https://api.vapi.ai"
    vapi_assistant_name: str = "NovaCRM tier confirmation"
    vapi_model: str = "gpt-4.1-mini"  # the LLM the voice assistant runs on (billed by Vapi)
    vapi_api_key: SecretStr | None = None
    vapi_assistant_id: str | None = None
    vapi_phone_number_id: str | None = None
    vapi_webhook_secret: SecretStr | None = None
    public_base_url: str | None = None

    # Twilio (only used to import the number into Vapi; calls go through Vapi)
    twilio_account_sid: str | None = None
    twilio_auth_token: SecretStr | None = None
    twilio_phone_number: str | None = Field(default=None, pattern=E164_PATTERN)

    # Call policy
    call_max_attempts: int = Field(default=3, ge=1, le=10)
    call_retry_delays_seconds: list[int] = Field(default_factory=lambda: [600, 1800])
    call_window_start: time = time(9, 0)
    call_window_end: time = time(19, 0)
    call_timezone: str = "Asia/Kolkata"
    call_days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])  # Mon=0 .. Sun=6
    call_max_duration_seconds: int = Field(default=240, ge=60, le=1800)
    # A call with no end-of-call report this long after its max duration is treated as failed.
    call_report_grace_seconds: int = Field(default=600, ge=60)

    # On shutdown, wait this long for running jobs before closing the database.
    shutdown_grace_seconds: float = Field(default=15.0, ge=0)

    # Recovery sweep (startup + periodic): re-queues work for deals left without a job.
    sweep_enabled: bool = True
    sweep_interval_seconds: int = Field(default=600, ge=60)

    # Rocketlane
    rocketlane_api_key: SecretStr | None = None
    rocketlane_base_url: str = "https://api.rocketlane.com/api/1.0"
    # Creating a project from a template took over 20s in a live test (Oct 2026); a
    # shorter timeout turns every create into an ambiguous outcome and a retry cycle.
    rocketlane_read_timeout_seconds: float = Field(default=90.0, gt=0)
    # Web app base URL for project links in Slack, e.g. https://acme.rocketlane.com
    rocketlane_app_url: str | None = None
    # Scheduled retries after in-request retries are exhausted, then the deal escalates.
    rocketlane_retry_delays_seconds: list[int] = Field(default_factory=lambda: [60, 300, 900])

    # Slack
    slack_bot_token: SecretStr | None = None
    slack_base_url: str = "https://slack.com/api"
    slack_connect_enabled: bool = False  # paid feature; off = customer invites are logged only
    # Onboarding channel steps that failed transiently are retried after these delays,
    # then the deal escalates.
    slack_retry_delays_seconds: list[int] = Field(default_factory=lambda: [60, 300])
    slack_escalation_channel: str = Field(
        default="cs-escalations", pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$"
    )

    # Escalation
    escalation_email: EmailStr | None = None
    # A notice that reached no channel is retried after these delays, then given up (logged).
    escalation_notice_retry_delays_seconds: list[int] = Field(default_factory=lambda: [300, 900])

    # HTTP
    http_connect_timeout_seconds: float = Field(default=5.0, gt=0)
    http_read_timeout_seconds: float = Field(default=20.0, gt=0)
    # In-request retries (exponential backoff with jitter). Scheduled retries are separate.
    http_max_attempts: int = Field(default=3, ge=1, le=6)
    http_backoff_base_seconds: float = Field(default=1.0, ge=0)
    http_backoff_max_seconds: float = Field(default=8.0, ge=0)
    # Per-integration cap on in-flight requests, so a burst of deals can't stampede a provider.
    http_max_concurrency: int = Field(default=4, ge=1, le=64)

    @field_validator(
        "call_retry_delays_seconds",
        "rocketlane_retry_delays_seconds",
        "escalation_notice_retry_delays_seconds",
        "slack_retry_delays_seconds",
    )
    @classmethod
    def _positive_delays(cls, v: list[int]) -> list[int]:
        if any(d <= 0 for d in v):
            raise ValueError("retry delays must be positive")
        return v

    @field_validator("allowed_sender_domains")
    @classmethod
    def _normalise_domains(cls, v: list[str]) -> list[str]:
        domains = [d.strip().lower().lstrip("@") for d in v]
        if any(not d or "@" in d or "." not in d for d in domains):
            raise ValueError("allowed_sender_domains entries must be bare domains, e.g. acme.com")
        return domains

    @field_validator("intake_required_fields")
    @classmethod
    def _known_intake_fields(cls, v: list[str]) -> list[str]:
        unknown = sorted(set(v) - set(INTAKE_FIELDS))
        if unknown:
            raise ValueError(f"unknown intake fields: {', '.join(unknown)}")
        return v

    @field_validator("call_days")
    @classmethod
    def _valid_days(cls, v: list[int]) -> list[int]:
        if not v or any(d not in range(7) for d in v):
            raise ValueError("call_days must be a non-empty list of weekday numbers 0-6")
        return sorted(set(v))

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
        if self.clarification_escalate_seconds <= self.clarification_reminder_seconds:
            raise ValueError("clarification_escalate_seconds must be after the reminder")
        if self.http_backoff_max_seconds < self.http_backoff_base_seconds:
            raise ValueError("http_backoff_max_seconds must be >= http_backoff_base_seconds")
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
    # Set once the template exists in Rocketlane. A deal for a tier without one is
    # escalated rather than created from the wrong template.
    rocketlane_template_id: int | None = None
    duration_days: PositiveInt
    csm_mode: Literal["dedicated", "pooled"]
    csm_pool: list[EmailStr] = Field(min_length=1)
    project_owner: EmailStr
    # Template placeholder (e.g. a "CSM" role) resolved to the chosen CSM, so the CSM's
    # tasks are assigned at creation. Optional: without it the CSM joins as a team member.
    csm_placeholder_id: int | None = None

    @model_validator(mode="after")
    def _pooled_has_one_assignee(self) -> Self:
        if self.csm_mode == "pooled" and len(self.csm_pool) != 1:
            raise ValueError("pooled tier must have exactly one shared CSM user")
        if len(set(self.csm_pool)) != len(self.csm_pool):
            raise ValueError("csm_pool contains duplicates")
        return self


class ProjectConfig(_Frozen):
    """How projects are created, independent of tier."""

    name_format: str = "{customer} - {tier} onboarding"
    # Inviting the customer contact emails them a Rocketlane invite. Off for demos with
    # made-up customer addresses.
    invite_customer_contact: bool = False
    # Rocketlane: assign tasks the template leaves unassigned to the project owner.
    assign_unassigned_tasks_to_owner: bool = False
    # Optional custom project fields to fill (text fields). Null = not used.
    plan_tier_field_id: int | None = None
    sf_opportunity_field_id: int | None = None

    @field_validator("name_format")
    @classmethod
    def _known_placeholders(cls, v: str) -> str:
        try:
            v.format(customer="x", tier="y")
        except (KeyError, IndexError, ValueError) as exc:
            raise ValueError("name_format may only use {customer} and {tier}") from exc
        return v


class TemplatesConfig(_Frozen):
    enterprise: TierConfig
    growth: TierConfig
    project: ProjectConfig = ProjectConfig()
    # Display names for owners and CSMs (email -> name), used in Slack copy.
    team: dict[EmailStr, str] = Field(default_factory=dict)

    def display_name(self, email: str) -> str:
        """The configured name, else one derived from the address (anita.csm -> Anita Csm)."""
        known = {k.lower(): v for k, v in self.team.items()}
        if (name := known.get(email.strip().lower())) is not None:
            return name
        local = email.split("@", 1)[0]
        return " ".join(part.capitalize() for part in re.split(r"[._+-]+", local) if part)

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
