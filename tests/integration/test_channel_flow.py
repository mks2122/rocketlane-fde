"""Onboarding channel end to end with a fake Slack and a fake Comms Agent."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlmodel import select

from app.config import Settings, TemplatesConfig
from app.db import Database
from app.domain import DealStatus, PlanTier
from app.errors import (
    AuthorizationError,
    IllegalTransitionError,
    OutcomeUnknownError,
    PermanentError,
    RetryableError,
)
from app.integrations.fakes import (
    FakeChatWorkspace,
    FakeCommsWriter,
    FakeJobScheduler,
    FakeTierVerifier,
    FakeVoiceCaller,
)
from app.models import AuditLog, Deal
from app.orchestrator.communication import open_channel
from app.orchestrator.escalation import escalate
from app.orchestrator.resolution import Resolution, ResolveAction, resolve
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition
from app.ports import ChannelCopy, ChannelRef
from app.services.slack_copy import channel_name

TEMPLATES = TemplatesConfig.model_validate(
    {
        "enterprise": {
            "rocketlane_template_id": 111,
            "duration_days": 30,
            "csm_mode": "dedicated",
            "csm_pool": ["paul@x.com"],
            "project_owner": "rachel@x.com",
        },
        "growth": {
            "rocketlane_template_id": 222,
            "duration_days": 14,
            "csm_mode": "pooled",
            "csm_pool": ["pod@x.com"],
            "project_owner": "rachel@x.com",
        },
        "team": {"paul@x.com": "Paul", "rachel@x.com": "Rachel"},
    }
)
GOOD = ChannelCopy(
    topic="Globex Corp · Enterprise onboarding · CSM Paul",
    welcome_message="Welcome Globex Corp! Enterprise plan; Paul is your dedicated CSM.",
)
BAD = ChannelCopy(topic="Hello", welcome_message="Welcome aboard @here!")


@pytest.fixture
def jobs() -> FakeJobScheduler:
    return FakeJobScheduler()


@pytest.fixture
def comms() -> FakeCommsWriter:
    return FakeCommsWriter()


@pytest.fixture
def rt(
    settings: Settings,
    db: Database,
    jobs: FakeJobScheduler,
    chat: FakeChatWorkspace,
    comms: FakeCommsWriter,
) -> Runtime:
    return Runtime(
        settings=settings.model_copy(
            update={
                "slack_retry_delays_seconds": [60, 300],
                "rocketlane_app_url": "https://acme.rocketlane.com/",
            }
        ),
        db=db,
        voice=FakeVoiceCaller(),
        tier_verifier=FakeTierVerifier(),
        jobs=jobs,
        chat=chat,
        templates=TEMPLATES,
        comms=comms,
    )


async def _project_created(rt: Runtime, tier: PlanTier = PlanTier.ENTERPRISE) -> Deal:
    async with rt.db.session() as session:
        deal = Deal(
            gmail_message_id="m1",
            gmail_thread_id="t1",
            sender_email="ravi@x.com",
            customer_company="Globex Corp",
            contact_name="Hank Scorpio",
            contact_email="hank@globex.com",
        )
        session.add(deal)
        await session.flush()
        for status in (DealStatus.AWAITING_TIER, DealStatus.CALLING, DealStatus.TIER_CONFIRMED):
            deal = await transition(
                session,
                deal,
                status,
                actor="t",
                rationale="setup",
                changes={"plan_tier": tier} if status is DealStatus.TIER_CONFIRMED else None,
            )
        return await transition(
            session,
            deal,
            DealStatus.PROJECT_CREATED,
            actor="t",
            rationale="setup",
            changes={"rocketlane_project_id": "5000000250111", "csm_email": "paul@x.com"},
        )


async def _deal(rt: Runtime, deal_id: str) -> Deal:
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
    assert deal is not None
    return deal


async def _source(rt: Runtime, deal_id: str) -> str | None:
    async with rt.db.session() as session:
        row = (
            await session.exec(
                select(AuditLog).where(
                    AuditLog.deal_id == deal_id, AuditLog.action == "open_channel"
                )
            )
        ).first()
    return (row.output_json or {}).get("copy_source") if row else None


# --- happy path --------------------------------------------------------------------


async def test_channel_is_opened_and_deal_completed(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter
) -> None:
    deal = await _project_created(rt)
    comms.drafts.append(GOOD)

    await open_channel(rt, deal.id)

    done = await _deal(rt, deal.id)
    assert done.status is DealStatus.COMPLETED
    name = channel_name("Globex Corp", deal.id)
    channel = chat.channels[name]
    assert done.slack_channel_id == channel.channel_id
    assert chat.topics[channel.channel_id] == GOOD.topic
    assert chat.messages[channel.channel_id] == [GOOD.welcome_message]
    assert chat.members[channel.channel_id] == {"paul@x.com", "rachel@x.com"}
    assert chat.external_invites[channel.channel_id] == {"hank@globex.com"}
    assert done.slack_welcome_ts is not None
    assert await _source(rt, deal.id) == "agent"
    [(facts, feedback)] = comms.called("write")
    assert facts.csm_name == "Paul"
    assert facts.project_url == "https://acme.rocketlane.com/projects/5000000250111"
    assert feedback == ()


# --- the copy never blocks the channel -----------------------------------------------


async def test_rejected_draft_is_retried_with_feedback(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter
) -> None:
    deal = await _project_created(rt)
    comms.drafts.extend([BAD, GOOD])

    await open_channel(rt, deal.id)

    _, feedback = comms.called("write")[1]
    assert any("@here" in point or "@channel" in point for point in feedback)
    assert await _source(rt, deal.id) == "agent_retry"
    assert [GOOD.welcome_message] in chat.messages.values()


async def test_two_bad_drafts_fall_back_to_the_template(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter
) -> None:
    deal = await _project_created(rt)
    comms.drafts.extend([BAD, BAD])

    await open_channel(rt, deal.id)

    assert await _source(rt, deal.id) == "template"
    [[message]] = list(chat.messages.values())
    assert "@here" not in message
    assert "Globex Corp" in message
    assert (await _deal(rt, deal.id)).status is DealStatus.COMPLETED


async def test_agent_down_falls_back_to_the_template(rt: Runtime, comms: FakeCommsWriter) -> None:
    deal = await _project_created(rt)
    comms.fail("write", PermanentError("comms_agent: HTTP 401"))

    await open_channel(rt, deal.id)

    assert await _source(rt, deal.id) == "template"
    assert (await _deal(rt, deal.id)).status is DealStatus.COMPLETED


# --- failures and resuming ---------------------------------------------------------


async def test_slack_down_retries_and_resumes_without_duplicates(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter, jobs: FakeJobScheduler
) -> None:
    deal = await _project_created(rt)
    comms.drafts.extend([GOOD, GOOD])
    chat.fail("invite_external", RetryableError("slack 503"))  # fails after the welcome post

    await open_channel(rt, deal.id, 1)
    assert (await _deal(rt, deal.id)).status is DealStatus.PARTIAL
    assert jobs.pop(f"channel:{deal.id}:2").args == (deal.id, "2")

    await open_channel(rt, deal.id, 2)

    assert (await _deal(rt, deal.id)).status is DealStatus.COMPLETED
    assert len(chat.channels) == 1
    assert sum(len(m) for m in chat.messages.values()) == 1  # welcome posted once


async def test_slack_retries_are_bounded(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter, jobs: FakeJobScheduler
) -> None:
    deal = await _project_created(rt)
    comms.drafts.extend([GOOD] * 3)
    chat.fail("find_channel", *[RetryableError("down")] * 3)

    for attempt in (1, 2):
        await open_channel(rt, deal.id, attempt)
        jobs.pop(f"channel:{deal.id}:{attempt + 1}")
    await open_channel(rt, deal.id, 3)

    assert (await _deal(rt, deal.id)).status is DealStatus.ESCALATED
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]


async def test_unconfirmed_welcome_is_not_posted_again(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter
) -> None:
    deal = await _project_created(rt)
    comms.drafts.append(GOOD)
    chat.fail("post_message", OutcomeUnknownError("timeout"))

    await open_channel(rt, deal.id)

    done = await _deal(rt, deal.id)
    assert done.status is DealStatus.COMPLETED
    assert done.slack_welcome_ts == "unconfirmed"


async def test_channel_created_earlier_is_reused(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter
) -> None:
    deal = await _project_created(rt)
    name = channel_name("Globex Corp", deal.id)
    chat.channels[name] = ChannelRef(
        channel_id="C_EXISTING", name=name
    )  # create succeeded, id lost
    comms.drafts.append(GOOD)

    await open_channel(rt, deal.id)

    assert (await _deal(rt, deal.id)).slack_channel_id == "C_EXISTING"
    assert not chat.called("create_channel")


async def test_team_member_without_slack_is_noted_not_fatal(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter
) -> None:
    deal = await _project_created(rt)
    comms.drafts.append(GOOD)
    chat.fail("invite_members", PermanentError("slack users.lookupByEmail: users_not_found"))

    await open_channel(rt, deal.id)

    assert (await _deal(rt, deal.id)).status is DealStatus.COMPLETED
    async with rt.db.session() as session:
        row = (await session.exec(select(AuditLog).where(AuditLog.action == "open_channel"))).one()
    assert row.output_json is not None
    assert "users_not_found" in row.output_json["notes"][0]


async def test_slack_auth_failure_escalates_at_once(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter, jobs: FakeJobScheduler
) -> None:
    deal = await _project_created(rt)
    comms.drafts.append(GOOD)
    chat.fail("find_channel", AuthorizationError("slack: invalid_auth"))

    await open_channel(rt, deal.id)

    assert (await _deal(rt, deal.id)).status is DealStatus.ESCALATED
    assert list(jobs.jobs) == [f"escalation-notice:{deal.id}:1"]


async def test_slack_not_configured_escalates(rt: Runtime) -> None:
    bare = replace(rt, chat=None)
    deal = await _project_created(bare)
    await open_channel(bare, deal.id)
    assert (await _deal(bare, deal.id)).status is DealStatus.ESCALATED


async def test_running_twice_does_nothing_the_second_time(
    rt: Runtime, chat: FakeChatWorkspace, comms: FakeCommsWriter
) -> None:
    deal = await _project_created(rt)
    comms.drafts.append(GOOD)
    await open_channel(rt, deal.id)
    await open_channel(rt, deal.id)
    assert len(chat.called("create_channel")) == 1


# --- human retry -------------------------------------------------------------------


async def test_human_can_retry_the_channel_only_when_a_project_exists(
    rt: Runtime, jobs: FakeJobScheduler
) -> None:
    deal = await _project_created(rt)
    async with rt.db.session() as session:
        deal = await transition(session, deal, DealStatus.PARTIAL, actor="t", rationale="t")
        await escalate(session, deal, actor="t", reason="Slack down")

    _, queued = await resolve(
        rt,
        deal.id,
        Resolution(action=ResolveAction.RETRY_CHANNEL, actor="Priya", reason="Slack is back"),
    )
    assert queued == "channel_queued"

    async with rt.db.session() as session:
        other = Deal(gmail_message_id="m2", gmail_thread_id="t2", sender_email="r@x.com")
        session.add(other)
        await session.flush()
        other = await transition(session, other, DealStatus.AWAITING_TIER, actor="t", rationale="t")
        await escalate(session, other, actor="t", reason="x")
    with pytest.raises(IllegalTransitionError, match="no Rocketlane project"):
        await resolve(
            rt,
            other.id,
            Resolution(action=ResolveAction.RETRY_CHANNEL, actor="Priya", reason="try again now"),
        )
