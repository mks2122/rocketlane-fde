from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, cast

from agents.tracing import custom_span, set_trace_processors, trace
from sqlmodel import select

from app.db import Database
from app.models import AgentSpan, AgentTrace
from app.tracing import SQLiteTraceProcessor


class _BrokenDatabase:
    @contextmanager
    def sync_session(self) -> Generator[Any, None, None]:
        raise OSError("disk unavailable")
        yield  # pragma: no cover


async def test_traces_and_spans_are_stored_locally(db: Database) -> None:
    set_trace_processors([SQLiteTraceProcessor(db)])

    with (
        trace("intake", group_id="d_42", metadata={"gmail_message_id": "m1"}) as t,
        custom_span("grounding_check", data={"fields": ["customer_company"]}),
    ):
        pass

    async with db.session() as session:
        stored = await session.get(AgentTrace, t.trace_id)
        assert stored is not None
        assert stored.name == "intake"
        assert stored.group_id == "d_42"
        assert stored.metadata_json == {"gmail_message_id": "m1"}
        assert stored.ended_at is not None

        span = (await session.exec(select(AgentSpan))).one()
        assert span.trace_id == t.trace_id
        assert span.span_type == "custom"
        assert span.data_json is not None
        assert span.data_json["name"] == "grounding_check"


def test_processor_failure_never_breaks_the_run() -> None:
    set_trace_processors([SQLiteTraceProcessor(cast(Database, _BrokenDatabase()))])

    with trace("intake", group_id="d_1"), custom_span("step"):
        pass  # must not raise
