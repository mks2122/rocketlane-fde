"""structlog configuration. Pretty console in dev, JSON in prod.

Bind per-deal context with `structlog.contextvars.bound_contextvars(deal_id=...)`
so every line emitted while handling a deal carries its id.
"""

from __future__ import annotations

import io
import logging
import sys
from typing import Any, cast

import structlog


def configure_logging(level: str = "INFO", *, json: bool = False) -> None:
    # On Windows a redirected stderr is cp1252: an email subject containing "→" would
    # otherwise raise UnicodeEncodeError from inside a log call.
    stream = sys.stderr
    if isinstance(stream, io.TextIOWrapper):
        cast("io.TextIOWrapper[Any]", stream).reconfigure(errors="backslashreplace")
    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            *shared,
            structlog.processors.format_exc_info if json else structlog.dev.set_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.PrintLoggerFactory(sys.stderr),
        cache_logger_on_first_use=True,
    )
    # Route stdlib logging (uvicorn, httpx, apscheduler) through the same level.
    logging.basicConfig(
        level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
