"""FastAPI application factory and lifespan."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from agents import add_trace_processor, set_default_openai_key
from apscheduler.schedulers.asyncio import (  # pyright: ignore[reportMissingTypeStubs]
    AsyncIOScheduler,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.agents.tier_verifier import AgentTierVerifier
from app.api import admin, webhooks
from app.config import Settings, get_settings, load_ae_directory, load_templates
from app.db import Database
from app.errors import ConfigError
from app.integrations.gmail import GmailMailbox
from app.integrations.slack import SlackWorkspace
from app.integrations.vapi import UnconfiguredVoice, VapiCaller
from app.log import configure_logging
from app.models import utcnow
from app.orchestrator.escalation import send_system_alert
from app.orchestrator.poller import InboxPoller
from app.orchestrator.runtime import Runtime, set_runtime
from app.ports import ChatWorkspace, JobScheduler, Mailbox, TierVerifier, VoiceCaller
from app.scheduler import MEMORY_STORE, APSchedulerJobs, create_scheduler
from app.tracing import SQLiteTraceProcessor

log = structlog.get_logger(__name__)


class HealthCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: list[HealthCheck]


def _inbox_check(poller: InboxPoller) -> HealthCheck:
    status = poller.status
    if status.last_error is not None:
        detail = status.last_error
    elif status.last_success_at is not None:
        detail = f"last successful poll {status.last_success_at.isoformat()}"
    else:
        detail = "no successful poll yet"
    return HealthCheck(name="gmail", ok=status.healthy, detail=detail)


def _mailbox(settings: Settings, override: Mailbox | None) -> Mailbox | None:
    """The CS inbox: polled for deals and used to send escalation emails."""
    if not settings.gmail_enabled:
        log.info("gmail_disabled", reason="GMAIL_ENABLED is false")
        return None
    if not (settings.allowed_sender_domains or settings.allowed_sender_emails):
        # Fail closed would silently ignore every deal; refuse to start instead.
        raise ConfigError("GMAIL_ENABLED needs ALLOWED_SENDER_DOMAINS or ALLOWED_SENDER_EMAILS")
    return override or GmailMailbox(settings)


def _start_inbox_polling(
    runtime: Runtime, scheduler: AsyncIOScheduler, mailbox: Mailbox | None
) -> InboxPoller | None:
    if mailbox is None:
        return None
    settings = runtime.settings

    async def alert_auth_failure(reason: str) -> None:
        await send_system_alert(
            runtime,
            subject="CS inbox disconnected",
            text=(
                "🚨 The onboarding system can no longer read the CS inbox, so new deals are "
                f"not being picked up.\nReason: {reason}\n"
                "Fix: run `uv run python scripts/gmail_auth.py`, then restart the service."
            ),
        )

    poller = InboxPoller(mailbox, runtime.db, settings, on_auth_failure=alert_auth_failure)
    scheduler.add_job(  # pyright: ignore[reportUnknownMemberType]
        poller.poll_once,
        "interval",
        seconds=settings.gmail_poll_seconds,
        id="poll_inbox",
        jobstore=MEMORY_STORE,
        next_run_time=utcnow(),
        replace_existing=True,
    )
    log.info("inbox_polling_enabled", every_s=settings.gmail_poll_seconds)
    return poller


def _chat(settings: Settings, override: ChatWorkspace | None) -> ChatWorkspace | None:
    if override is not None:
        return override
    if settings.slack_bot_token is None:
        log.warning("slack_disabled", reason="SLACK_BOT_TOKEN not set")
        return None
    return SlackWorkspace(settings)


def _warn_if_escalations_unreachable(runtime: Runtime) -> None:
    email = runtime.mailbox is not None and runtime.settings.escalation_email is not None
    if runtime.chat is None and not email:
        log.error(
            "escalations_unreachable",
            fix="set SLACK_BOT_TOKEN, or GMAIL_ENABLED with ESCALATION_EMAIL",
        )


def _voice_caller(settings: Settings) -> VoiceCaller:
    if settings.vapi_api_key and settings.vapi_assistant_id and settings.vapi_phone_number_id:
        return VapiCaller(settings)
    log.warning("voice_calls_disabled", reason="Vapi settings incomplete")
    return UnconfiguredVoice()


def _configure_openai(settings: Settings) -> None:
    if settings.openai_api_key is None:
        log.warning("openai_not_configured", effect="tier verification will escalate to a human")
        return
    set_default_openai_key(settings.openai_api_key.get_secret_value())


def create_app(
    settings: Settings | None = None,
    *,
    mailbox: Mailbox | None = None,
    chat: ChatWorkspace | None = None,
    voice: VoiceCaller | None = None,
    tier_verifier: TierVerifier | None = None,
    jobs: JobScheduler | None = None,
) -> FastAPI:
    """Build the app. The keyword arguments replace real adapters (tests, offline demos)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        configure_logging(settings.log_level, json=settings.log_json)
        # Validate business config at startup: a bad YAML must stop the service,
        # not surface later in the middle of a deal.
        app.state.templates = load_templates(settings.templates_path)
        app.state.ae_directory = load_ae_directory(settings.ae_directory_path)

        db = Database(settings.database_url)
        await db.create_all()
        app.state.db = db
        add_trace_processor(SQLiteTraceProcessor(db))

        _configure_openai(settings)
        scheduler = create_scheduler(db)
        voice_caller = voice or _voice_caller(settings)
        runtime = Runtime(
            settings=settings,
            db=db,
            voice=voice_caller,
            tier_verifier=tier_verifier or AgentTierVerifier(settings),
            jobs=jobs or APSchedulerJobs(scheduler),
            mailbox=_mailbox(settings, mailbox),
            chat=_chat(settings, chat),
        )
        set_runtime(runtime)
        app.state.runtime = runtime
        app.state.poller = _start_inbox_polling(runtime, scheduler, runtime.mailbox)
        _warn_if_escalations_unreachable(runtime)
        scheduler.start()
        log.info("startup_complete", env=settings.env)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            if isinstance(voice_caller, VapiCaller):
                await voice_caller.aclose()
            if isinstance(runtime.chat, SlackWorkspace):
                await runtime.chat.aclose()
            set_runtime(None)
            await db.dispose()
            log.info("shutdown_complete")

    app = FastAPI(title="NovaCRM Onboarding Agents", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.include_router(webhooks.router)
    app.include_router(admin.router)

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        db: Database = request.app.state.db
        checks: list[HealthCheck] = []
        try:
            await db.ping()
            checks.append(HealthCheck(name="database", ok=True))
        except Exception as exc:
            checks.append(HealthCheck(name="database", ok=False, detail=type(exc).__name__))

        poller: InboxPoller | None = request.app.state.poller
        if poller is not None:
            checks.append(_inbox_check(poller))

        healthy = all(c.ok for c in checks)
        body = HealthResponse(status="ok" if healthy else "degraded", checks=checks)
        return JSONResponse(body.model_dump(), status_code=200 if healthy else 503)

    return app
