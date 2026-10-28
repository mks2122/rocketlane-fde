"""Agents SDK tracing: keep a local copy of every trace and span in the database.

The SDK's default OpenAI exporter stays installed alongside this processor, so
traces appear both in the OpenAI dashboard and at GET /deals/{id}/traces.
Processor callbacks are synchronous, so they write through the sync engine.
A failure here must never break an agent run: every callback swallows and logs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import structlog
from agents.tracing import Span, Trace, TracingProcessor

from app.db import Database
from app.models import AgentSpan, AgentTrace
from app.services.audit import to_jsonable

log = structlog.get_logger(__name__)


def _as_dict(value: Any) -> dict[str, Any] | None:
    data = to_jsonable(value)
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


class SQLiteTraceProcessor(TracingProcessor):
    def __init__(self, db: Database) -> None:
        self._db = db

    def on_trace_start(self, trace: Trace) -> None:
        try:
            exported = trace.export() or {}
            with self._db.sync_session() as session:
                session.merge(
                    AgentTrace(
                        trace_id=trace.trace_id,
                        name=trace.name,
                        group_id=exported.get("group_id"),
                        metadata_json=_as_dict(exported.get("metadata")),
                    )
                )
        except Exception:
            log.exception("trace_store_failed", trace_id=trace.trace_id)

    def on_trace_end(self, trace: Trace) -> None:
        try:
            with self._db.sync_session() as session:
                row = session.get(AgentTrace, trace.trace_id)
                if row is not None:
                    row.ended_at = datetime.now(UTC)
        except Exception:
            log.exception("trace_store_failed", trace_id=trace.trace_id)

    def on_span_start(self, span: Span[Any]) -> None:
        return None

    def on_span_end(self, span: Span[Any]) -> None:
        try:
            exported = span.export() or {}
            data = _as_dict(exported.get("span_data")) or {}
            with self._db.sync_session() as session:
                session.merge(
                    AgentSpan(
                        span_id=span.span_id,
                        trace_id=span.trace_id,
                        parent_id=span.parent_id,
                        span_type=str(data.get("type", "unknown")),
                        started_at=span.started_at,
                        ended_at=span.ended_at,
                        data_json=data,
                        error_json=_as_dict(exported.get("error")),
                    )
                )
        except Exception:
            log.exception("span_store_failed", span_id=span.span_id)

    def shutdown(self) -> None:
        return None

    def force_flush(self) -> None:
        return None
