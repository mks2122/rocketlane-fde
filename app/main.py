"""FastAPI application factory and lifespan."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Literal

import structlog
from agents import add_trace_processor
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.config import Settings, get_settings, load_ae_directory, load_templates
from app.db import Database
from app.log import configure_logging
from app.tracing import SQLiteTraceProcessor

log = structlog.get_logger(__name__)


class HealthCheck(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    checks: list[HealthCheck]


def create_app(settings: Settings | None = None) -> FastAPI:
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
        log.info("startup_complete", env=settings.env)
        try:
            yield
        finally:
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

        healthy = all(c.ok for c in checks)
        body = HealthResponse(status="ok" if healthy else "degraded", checks=checks)
        return JSONResponse(body.model_dump(), status_code=200 if healthy else 503)

    return app
