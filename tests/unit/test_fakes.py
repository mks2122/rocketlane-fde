"""The fakes stand in for real systems in every integration test, so their own
behaviour (fault injection, idempotency, ordering) is pinned down here."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.domain import PlanTier
from app.errors import OutcomeUnknownError, PermanentError, RetryableError
from app.integrations.fakes import (
    FakeChatWorkspace,
    FakeMailbox,
    FakeProjectStore,
    FakeVoiceCaller,
)
from app.ports import InboundEmail, NewProject, OutboundCall, OutboundEmail

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)


def _email(message_id: str, minutes: int = 0) -> InboundEmail:
    return InboundEmail(
        message_id=message_id,
        thread_id=f"t-{message_id}",
        from_email="ravi.ae@example.com",
        subject="New deal: Acme",
        body_text="Customer: Acme",
        received_at=T0 + timedelta(minutes=minutes),
    )


def _project(deal_id: str = "d_1") -> NewProject:
    return NewProject(
        deal_id=deal_id,
        project_name="Acme - Enterprise onboarding",
        customer_company="Acme",
        plan_tier=PlanTier.ENTERPRISE,
        template_id=1,
        start_date=date(2026, 10, 28),
        owner_email="priya@example.com",
        csm_email="anita@example.com",
    )


async def test_mailbox_lists_unprocessed_oldest_first(mailbox: FakeMailbox) -> None:
    mailbox.deliver(_email("b", minutes=5))
    mailbox.deliver(_email("a", minutes=1))
    assert await mailbox.list_new_message_ids() == ["a", "b"]
    await mailbox.mark_processed("a")
    await mailbox.mark_processed("a")  # idempotent
    assert await mailbox.list_new_message_ids() == ["b"]


async def test_mailbox_fetch_unknown_is_permanent(mailbox: FakeMailbox) -> None:
    with pytest.raises(PermanentError):
        await mailbox.fetch("missing")


async def test_faults_are_raised_in_order_then_cleared(mailbox: FakeMailbox) -> None:
    mailbox.fail("send", RetryableError("first"), PermanentError("second"))
    email = OutboundEmail(to=("a@example.com",), subject="s", body_text="b")
    with pytest.raises(RetryableError, match="first"):
        await mailbox.send(email)
    with pytest.raises(PermanentError, match="second"):
        await mailbox.send(email)
    assert await mailbox.send(email) == "sent_1"
    assert len(mailbox.called("send")) == 3
    assert mailbox.sent == [email]


async def test_voice_records_calls_with_unique_ids(voice: FakeVoiceCaller) -> None:
    call = OutboundCall(
        deal_id="d_1",
        attempt_no=1,
        to_number="+919876543210",
        ae_name="Ravi",
        customer_company="Acme",
    )
    assert await voice.start_call(call) != await voice.start_call(call)
    assert voice.placed == [call, call]


def test_outbound_call_rejects_non_e164_number() -> None:
    with pytest.raises(ValidationError):
        OutboundCall(
            deal_id="d", attempt_no=1, to_number="98765 43210", ae_name="R", customer_company="A"
        )


async def test_project_store_marks_created_projects(projects: FakeProjectStore) -> None:
    ref = await projects.create_project(_project("d_42"))
    assert ref.deal_marker == "d_42"
    assert await projects.list_projects() == [ref]


async def test_project_created_but_response_lost(projects: FakeProjectStore) -> None:
    # The scenario the marker check exists for: the create succeeded upstream,
    # but we only saw a timeout.
    projects.lose_next_response(OutcomeUnknownError("read timeout"))
    with pytest.raises(OutcomeUnknownError):
        await projects.create_project(_project("d_7"))
    markers = [p.deal_marker for p in await projects.list_projects()]
    assert markers == ["d_7"]


async def test_chat_channel_names_are_unique(chat: FakeChatWorkspace) -> None:
    ref = await chat.create_channel("onb-acme")
    assert await chat.find_channel("onb-acme") == ref
    with pytest.raises(PermanentError, match="name taken"):
        await chat.create_channel("onb-acme")


async def test_chat_invites_are_idempotent_and_case_insensitive(chat: FakeChatWorkspace) -> None:
    ref = await chat.create_channel("onb-acme")
    await chat.invite_members(ref.channel_id, ["CSM@example.com"])
    await chat.invite_members(ref.channel_id, ["csm@example.com"])
    assert chat.members[ref.channel_id] == {"csm@example.com"}


async def test_fake_thread_includes_our_replies_in_order(mailbox: FakeMailbox) -> None:
    mailbox.deliver(_email("a"))
    first = await mailbox.fetch("a")
    await mailbox.send(
        OutboundEmail(
            to=("ravi.ae@example.com",),
            subject="Re",
            body_text="Which plan?",
            thread_id=first.thread_id,
        )
    )
    thread = await mailbox.fetch_thread(first.thread_id)
    assert [(e.message_id, e.from_self) for e in thread] == [("a", False), ("sent_1", True)]
    assert thread[1].received_at > thread[0].received_at
