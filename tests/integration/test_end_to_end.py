"""The whole workflow, end to end (Part 4 happy path).

Deal email → inbox poll → intake → tier call → Vapi webhooks → two-key decision →
Rocketlane project from the tier's template → Slack channel → COMPLETED.

Only the edges are fakes (Gmail, Vapi, the three agents, Rocketlane, Slack). Every queued
job runs through its real entry point in `app.orchestrator.jobs`, exactly as APScheduler
would call it, until nothing is left in the queue.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable, Iterator
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from app.config import AccountExecutive, AEDirectory, Settings, TemplatesConfig
from app.db import Database
from app.domain import DealStatus, PlanTier, TierAnswer
from app.integrations.fakes import (
    FakeChatWorkspace,
    FakeCommsWriter,
    FakeIntakeExtractor,
    FakeJobScheduler,
    FakeMailbox,
    FakeProjectStore,
    FakeTierVerifier,
    FakeVoiceCaller,
)
from app.integrations.vapi import VapiWebhook
from app.models import Deal
from app.orchestrator import voice
from app.orchestrator.poller import InboxPoller
from app.orchestrator.progress import advance
from app.orchestrator.runtime import Runtime, set_runtime
from app.ports import ChannelCopy, DealIntake, ExtractedField, TierVerdict
from tests.factories import AE_EMAIL, inbound_email

SF = "https://novacrm.lightning.force.com/lightning/r/Opportunity/006Hs00001AbCdE/view"
EMAIL = f"""Hi team, we just closed Globex Corp!
Main contact: Hank Scorpio (hank@globex.com).
SF: {SF}
Thanks, Ravi Kumar"""
TRANSCRIPT = (
    "AI: Which plan did Globex Corp sign up for: Enterprise or Growth?\n"
    "User: Enterprise.\nAI: Just to confirm, Enterprise, correct?\nUser: Yes, correct."
)
WED_10AM = datetime(2026, 10, 28, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(UTC)
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
        "team": {"paul@x.com": "Paul"},
    }
)


def _field(value: str, evidence: str) -> ExtractedField:
    return ExtractedField(value=value, evidence=evidence)


INTAKE = DealIntake(
    is_deal_handoff=True,
    customer_company=_field("Globex Corp", "we just closed Globex Corp"),
    contact_name=_field("Hank Scorpio", "Main contact: Hank Scorpio"),
    contact_email=_field("hank@globex.com", "Hank Scorpio (hank@globex.com)"),
    ae_name=_field("Ravi Kumar", "Thanks, Ravi Kumar"),
    sf_opportunity_url=_field(SF, f"SF: {SF}"),
    notes=None,
)


@pytest.fixture
def parts(
    settings: Settings, db: Database, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    monkeypatch.setattr(voice, "utcnow", lambda: WED_10AM)  # inside the calling window
    chat = FakeChatWorkspace()
    parts: dict[str, Any] = {
        "mailbox": FakeMailbox(),
        "voice": FakeVoiceCaller(),
        "verifier": FakeTierVerifier(
            TierVerdict(
                tier=TierAnswer.ENTERPRISE,
                ae_confirmed=True,
                evidence_quote="Yes, correct",
                rationale="r",
            )
        ),
        "intake": FakeIntakeExtractor(INTAKE),
        "projects": FakeProjectStore(),
        "chat": chat,
        "comms": FakeCommsWriter(
            ChannelCopy(
                topic="Globex Corp · Enterprise onboarding",
                welcome_message="Welcome Globex Corp! Enterprise plan; Paul is your dedicated CSM.",
            )
        ),
        "jobs": FakeJobScheduler(),
    }
    runtime = Runtime(
        settings=settings,
        db=db,
        voice=parts["voice"],
        tier_verifier=parts["verifier"],
        jobs=parts["jobs"],
        mailbox=parts["mailbox"],
        chat=chat,
        intake=parts["intake"],
        ae_directory=AEDirectory(
            account_executives=[
                AccountExecutive(name="Ravi Kumar", email=AE_EMAIL, phone="+919876543210")
            ]
        ),
        projects=parts["projects"],
        templates=TEMPLATES,
        comms=parts["comms"],
    )
    set_runtime(runtime)
    parts["rt"] = runtime
    yield parts
    set_runtime(None)


async def _drain(
    jobs: FakeJobScheduler, *, stop_at: str | None = None, limit: int = 20
) -> list[str]:
    """Run queued jobs through their real entry points, oldest first, until none are left."""
    ran: list[str] = []
    for _ in range(limit):
        if not jobs.jobs:
            return ran
        job_id = min(jobs.jobs, key=lambda j: jobs.jobs[j].run_at)
        if stop_at is not None and job_id.startswith(stop_at):
            return ran
        job = jobs.pop(job_id)
        module, name = job.job.split(":")
        entry = cast(Callable[..., Awaitable[None]], getattr(importlib.import_module(module), name))
        await entry(*job.args)
        ran.append(job_id)
    raise AssertionError(f"jobs never drained: {list(jobs.jobs)}")


async def test_deal_email_to_onboarding_channel(parts: dict[str, Any]) -> None:
    rt: Runtime = parts["rt"]
    mailbox: FakeMailbox = parts["mailbox"]
    jobs: FakeJobScheduler = parts["jobs"]

    async def queue_next(deal_id: str) -> None:
        async with rt.db.session() as session:
            deal = await session.get(Deal, deal_id)
        assert deal is not None
        await advance(rt, deal)

    # 1. The AE's email arrives and is polled.
    mailbox.deliver(inbound_email("m1", thread_id="t1", body=EMAIL))
    await InboxPoller(mailbox, rt.db, rt.settings, after_inbound=queue_next).poll_once()

    # 2. Intake, then the call is placed; stop when the call is in progress.
    ran = await _drain(jobs, stop_at="process-call:")
    assert [j.split(":")[0] for j in ran] == ["intake", "call"]
    [call] = parts["voice"].placed
    assert (call.to_number, call.customer_company) == ("+919876543210", "Globex Corp")

    # 3. Vapi reports the AE's answer and the end of the call.
    async with rt.db.session() as session:
        deal = (await session.exec(select(Deal))).one()
    call_id = "call_1"
    tool_call = {
        "type": "tool-calls",
        "call": {"id": call_id},
        "toolCallList": [
            {
                "id": "tc1",
                "function": {
                    "name": "record_plan_tier",
                    "arguments": {
                        "tier": "enterprise",
                        "ae_confirmed": True,
                        "ae_quote": "Enterprise",
                    },
                },
            }
        ],
    }
    await voice.record_tool_calls(rt, VapiWebhook.model_validate({"message": tool_call}).message)
    report = {
        "type": "end-of-call-report",
        "endedReason": "assistant-ended-call",
        "call": {"id": call_id},
        "artifact": {"transcript": TRANSCRIPT},
    }
    await voice.record_end_of_call(rt, VapiWebhook.model_validate({"message": report}).message)

    # 4. Decision, project, channel.
    ran = await _drain(jobs)
    assert [j.split(":")[0] for j in ran] == ["process-call", "project", "channel"]

    async with rt.db.session() as session:
        done = await session.get(Deal, deal.id)
    assert done is not None
    assert done.status is DealStatus.COMPLETED
    assert done.plan_tier is PlanTier.ENTERPRISE
    [project] = parts["projects"].created
    assert (project.template_id, project.csm_email, project.owner_email) == (
        111,
        "paul@x.com",
        "rachel@x.com",
    )
    assert done.rocketlane_project_id is not None
    chat: FakeChatWorkspace = parts["chat"]
    [channel] = chat.channels.values()
    assert done.slack_channel_id == channel.channel_id
    assert chat.messages[channel.channel_id] == [
        "Welcome Globex Corp! Enterprise plan; Paul is your dedicated CSM."
    ]
    assert not mailbox.sent  # nothing was missing: no clarification email
