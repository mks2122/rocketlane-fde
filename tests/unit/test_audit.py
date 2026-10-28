from __future__ import annotations

import pytest
from agents.tracing import trace
from pydantic import SecretStr
from sqlmodel import select

from app.db import Database
from app.models import AuditLog
from app.services.audit import MAX_STRING_CHARS, REDACTED, audit_step, to_jsonable


class BoomError(RuntimeError):
    pass


def test_to_jsonable_masks_secrets_and_sensitive_keys() -> None:
    data = to_jsonable({"api_key": "sk-123", "nested": {"token": "abc"}, "pw": SecretStr("x")})
    assert data == {"api_key": REDACTED, "nested": {"token": REDACTED}, "pw": REDACTED}


def test_to_jsonable_truncates_long_strings() -> None:
    out = to_jsonable("a" * (MAX_STRING_CHARS + 50))
    assert isinstance(out, str)
    assert out.startswith("a" * MAX_STRING_CHARS)
    assert "truncated 50 chars" in out


async def test_success_row_commits_with_callers_transaction(db: Database) -> None:
    async with (
        db.session() as session,
        audit_step(
            db, session, deal_id="d_1", actor="intake_agent", action="parse", input={"id": "m1"}
        ) as step,
    ):
        step.output = {"customer_company": "Acme"}
        step.rationale = "all fields grounded"

    async with db.session() as session:
        row = (await session.exec(select(AuditLog))).one()
        assert row.status == "ok"
        assert row.input_json == {"id": "m1"}
        assert row.output_json == {"customer_company": "Acme"}
        assert row.rationale == "all fields grounded"
        assert row.latency_ms is not None


async def _ok_step_then_later_failure(db: Database) -> None:
    async with db.session() as session:
        async with audit_step(db, session, deal_id="d_1", actor="a", action="ok_step"):
            pass
        raise BoomError("later step failed")


async def _failing_step(db: Database) -> None:
    async with (
        db.session() as session,
        audit_step(db, session, deal_id="d_1", actor="rocketlane", action="create"),
    ):
        raise BoomError("503 from Rocketlane")


async def test_success_row_is_rolled_back_with_callers_transaction(db: Database) -> None:
    with pytest.raises(BoomError):
        await _ok_step_then_later_failure(db)

    async with db.session() as session:
        assert (await session.exec(select(AuditLog))).all() == []


async def test_error_row_survives_rollback(db: Database) -> None:
    with pytest.raises(BoomError):
        await _failing_step(db)

    async with db.session() as session:
        row = (await session.exec(select(AuditLog))).one()
        assert row.status == "error"
        assert row.error == "BoomError: 503 from Rocketlane"


async def test_trace_id_is_captured(db: Database) -> None:
    with trace("intake", group_id="d_1") as t:
        async with (
            db.session() as session,
            audit_step(db, session, deal_id="d_1", actor="a", action="b"),
        ):
            pass

    async with db.session() as session:
        row = (await session.exec(select(AuditLog))).one()
        assert row.trace_id == t.trace_id
