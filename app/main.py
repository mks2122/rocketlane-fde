"""FastAPI application factory and lifespan."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from agents import add_trace_processor
from apscheduler.schedulers.asyncio import (  # pyright: ignore[reportMissingTypeStubs]
    AsyncIOScheduler,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import Settings, get_settings, load_ae_directory, load_templates
from app.db import Database
from app.errors import ConfigError
from app.integrations.gmail import GmailMailbox
from app.log import configure_logging
from app.models import utcnow
from app.orchestrator.poller import InboxPoller
from app.ports import Mailbox
from app.scheduler import MEMORY_STORE, create_scheduler
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


def _start_inbox_polling(
    settings: Settings, db: Database, scheduler: AsyncIOScheduler, mailbox: Mailbox | None
) -> InboxPoller | None:
    if not settings.gmail_enabled:
        log.info("inbox_polling_disabled", reason="GMAIL_ENABLED is false")
        return None
    if not (settings.allowed_sender_domains or settings.allowed_sender_emails):
        # Fail closed would silently ignore every deal; refuse to start instead.
        raise ConfigError("GMAIL_ENABLED needs ALLOWED_SENDER_DOMAINS or ALLOWED_SENDER_EMAILS")
    poller = InboxPoller(mailbox or GmailMailbox(settings), db, settings)
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


def create_app(settings: Settings | None = None, *, mailbox: Mailbox | None = None) -> FastAPI:
    """Build the app. `mailbox` overrides the Gmail adapter (tests, offline demos)."""
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

        scheduler = create_scheduler(db)
        app.state.poller = _start_inbox_polling(settings, db, scheduler, mailbox)
        scheduler.start()
        log.info("startup_complete", env=settings.env)
        try:
            yield
        finally:
            scheduler.shutdown(wait=False)
            await db.dispose()
            log.info("shutdown_complete")

    app = FastAPI(title="NovaCRM Onboarding Agents", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings

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
