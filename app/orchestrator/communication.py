"""Communication step: open the customer's onboarding Slack channel and welcome them.

    PROJECT_CREATED ─ open_channel ─┬─ channel, topic, welcome posted ─► COMPLETED
                                    ├─ Slack down / ambiguous         ─► PARTIAL (+ retry job)
                                    └─ auth/config error, retries out ─► PARTIAL → ESCALATED

Every step is idempotent and its result is stored as it happens, so a retry resumes
where the last attempt stopped:
- the channel name is deterministic per deal, and its id is saved once created;
- the welcome message is posted once (`slack_welcome_ts`); a post whose outcome is
  unknown is recorded as unconfirmed and not posted again, since two welcome
  messages are worse than one unconfirmed one;
- internal invites are best effort: someone without a Slack account is noted, not fatal;
- the Slack Connect invite to the customer is mocked unless SLACK_CONNECT_ENABLED.

The copy comes from the Comms Agent, validated, with one retry carrying the validation
feedback, then the fixed template. The LLM never blocks the channel.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Final

import structlog
from agents import trace
from sqlmodel import select

from app.domain import DealStatus
from app.errors import (
    AuthorizationError,
    IntegrationError,
    OutcomeUnknownError,
    PermanentError,
    RetryableError,
)
from app.models import AuditLog, Deal, utcnow
from app.orchestrator.escalation import escalate, queue_escalation_notice
from app.orchestrator.runtime import Runtime
from app.orchestrator.state_machine import transition, update_fields
from app.ports import ChannelCopy, ChannelFacts, ChatWorkspace
from app.services.audit import audit_step
from app.services.slack_copy import channel_name, fallback_copy, problems_with

log = structlog.get_logger(__name__)

ACTOR: Final = "comms_agent"
CHANNEL_JOB: Final = "app.orchestrator.jobs:channel_job"
UNCONFIRMED: Final = "unconfirmed"


def queue_channel_setup(rt: Runtime, deal_id: str, *, attempt: int = 1, delay_s: int = 0) -> None:
    rt.jobs.schedule(
        CHANNEL_JOB,
        run_at=utcnow() + timedelta(seconds=delay_s),
        args=[deal_id, str(attempt)],
        job_id=f"channel:{deal_id}:{attempt}",
    )


async def open_channel(rt: Runtime, deal_id: str, attempt: int = 1) -> None:
    """Set up the onboarding channel for a deal whose project exists. Safe to run twice."""
    async with rt.db.session() as session:
        deal = await session.get(Deal, deal_id)
        if deal is not None and deal.status is DealStatus.PARTIAL:
            deal = await transition(
                session,
                deal,
                DealStatus.PROJECT_CREATED,
                actor=ACTOR,
                rationale=f"retrying the Slack channel (attempt {attempt})",
            )
    if deal is None or deal.status is not DealStatus.PROJECT_CREATED:
        log.info("channel_skipped", deal_id=deal_id, status=deal.status if deal else None)
        return

    with (
        structlog.contextvars.bound_contextvars(deal_id=deal_id),
        trace("communication", group_id=deal_id),
    ):
        if rt.chat is None or rt.templates is None:
            await _give_up(rt, deal, "Slack is not configured (SLACK_BOT_TOKEN)")
            return
        facts = _facts(rt, deal)
        copy, source = await _draft(rt, facts)
        notes: list[str] = []
        try:
            deal = await _ensure_channel(rt, rt.chat, deal)
            assert deal.slack_channel_id is not None  # noqa: S101  # set by _ensure_channel
            channel_id = deal.slack_channel_id
            await rt.chat.set_topic(channel_id, copy.topic)
            notes += await _invite_team(rt.chat, channel_id, [facts.csm_email, facts.owner_email])
            deal = await _post_welcome(rt, rt.chat, deal, channel_id, copy)
            notes += await _invite_customer(rt.chat, channel_id, deal.contact_email)
        except RetryableError as exc:
            await _retry_later(rt, deal, attempt, f"Slack step failed: {exc}")
            return
        except IntegrationError as exc:
            await _give_up(rt, deal, f"Slack rejected the channel setup: {exc}")
            return

        summary: dict[str, Any] = {
            "channel": channel_name(facts.customer_company, deal.id),
            "channel_id": channel_id,
            "copy_source": source,
            "welcome_ts": deal.slack_welcome_ts,
            "notes": notes,
        }
        async with (
            rt.db.session() as session,
            audit_step(
                rt.db,
                session,
                deal_id=deal.id,
                actor=ACTOR,
                action="open_channel",
                input={"attempt": attempt, "facts": facts},
            ) as step,
        ):
            await transition(
                session,
                deal,
                DealStatus.COMPLETED,
                actor=ACTOR,
                rationale=f"onboarding channel ready ({source} copy)",
            )
            step.output, step.rationale = summary, "channel, topic and welcome message in place"
        log.info("channel_ready", channel_id=channel_id, copy_source=source, notes=notes)


def _facts(rt: Runtime, deal: Deal) -> ChannelFacts:
    assert rt.templates is not None  # noqa: S101  # checked by the caller
    assert deal.plan_tier is not None  # noqa: S101  # PROJECT_CREATED implies a tier
    tier_config = rt.templates.for_tier(deal.plan_tier)
    csm = deal.csm_email or tier_config.csm_pool[0]
    project_url = None
    if rt.settings.rocketlane_app_url and deal.rocketlane_project_id:
        project_url = (
            f"{rt.settings.rocketlane_app_url.rstrip('/')}/projects/{deal.rocketlane_project_id}"
        )
    return ChannelFacts(
        customer_company=deal.customer_company or "the customer",
        tier=deal.plan_tier,
        duration_days=tier_config.duration_days,
        csm_name=rt.templates.display_name(csm),
        csm_email=csm,
        owner_email=tier_config.project_owner,
        contact_name=deal.contact_name,
        project_url=project_url,
    )


async def _draft(rt: Runtime, facts: ChannelFacts) -> tuple[ChannelCopy, str]:
    """Agent draft, one corrected retry, then the fixed template. Never raises."""
    if rt.comms is None:
        return fallback_copy(facts), "template"
    feedback: list[str] = []
    for source in ("agent", "agent_retry"):
        try:
            draft = await rt.comms.write(facts, feedback=feedback)
        except IntegrationError as exc:
            log.warning("comms_agent_unavailable", error=str(exc))
            break
        feedback = problems_with(draft, facts)
        if not feedback:
            return draft, source
        log.warning("comms_draft_rejected", problems=feedback)
    return fallback_copy(facts), "template"


async def _ensure_channel(rt: Runtime, chat: ChatWorkspace, deal: Deal) -> Deal:
    if deal.slack_channel_id is not None:
        return deal
    name = channel_name(deal.customer_company or "customer", deal.id)
    channel = await chat.find_channel(name)
    if channel is None:
        try:
            channel = await chat.create_channel(name)
        except OutcomeUnknownError:
            # It may exist now; the next attempt's find_channel will tell.
            raise
        except PermanentError as exc:
            if "name_taken" not in str(exc):
                raise
            channel = await chat.find_channel(name)  # created by a concurrent attempt
            if channel is None:
                raise
    async with rt.db.session() as session:
        return await update_fields(
            session,
            deal,
            actor=ACTOR,
            rationale=f"onboarding channel #{channel.name}",
            changes={"slack_channel_id": channel.channel_id},
        )


async def _invite_team(chat: ChatWorkspace, channel_id: str, emails: list[str]) -> list[str]:
    notes: list[str] = []
    for email in dict.fromkeys(emails):  # de-duplicated, order kept
        try:
            await chat.invite_members(channel_id, [email])
        except AuthorizationError:
            raise
        except PermanentError as exc:
            # Usually "users_not_found": they have no Slack account. Not worth failing the deal.
            notes.append(f"could not invite {email}: {exc}")
    return notes


async def _post_welcome(
    rt: Runtime, chat: ChatWorkspace, deal: Deal, channel_id: str, copy: ChannelCopy
) -> Deal:
    if deal.slack_welcome_ts is not None:
        return deal
    try:
        ts = await chat.post_message(channel_id, copy.welcome_message)
    except OutcomeUnknownError as exc:
        log.warning("welcome_post_unconfirmed", error=str(exc))
        ts = UNCONFIRMED
    async with rt.db.session() as session:
        return await update_fields(
            session,
            deal,
            actor=ACTOR,
            rationale="welcome message posted",
            changes={"slack_welcome_ts": ts},
        )


async def _invite_customer(chat: ChatWorkspace, channel_id: str, email: str | None) -> list[str]:
    if not email:
        return ["no customer contact email; Slack Connect invite skipped"]
    try:
        await chat.invite_external(channel_id, email)
    except AuthorizationError:
        raise
    except PermanentError as exc:
        return [f"Slack Connect invite to {email} failed: {exc}"]
    return []


async def _retry_later(rt: Runtime, deal: Deal, attempt: int, reason: str) -> None:
    delays = rt.settings.slack_retry_delays_seconds
    if attempt > len(delays):
        await _give_up(rt, deal, f"Slack still failing after {attempt} attempts; last: {reason}")
        return
    delay = delays[attempt - 1]
    async with rt.db.session() as session:
        await transition(
            session,
            deal,
            DealStatus.PARTIAL,
            actor=ACTOR,
            rationale=f"{reason}; attempt {attempt + 1} in {delay}s",
        )
    queue_channel_setup(rt, deal.id, attempt=attempt + 1, delay_s=delay)


async def _give_up(rt: Runtime, deal: Deal, reason: str) -> None:
    async with rt.db.session() as session:
        partial = deal
        if deal.status is DealStatus.PROJECT_CREATED:
            partial = await transition(
                session, deal, DealStatus.PARTIAL, actor=ACTOR, rationale=reason
            )
        await escalate(
            session, partial, actor=ACTOR, reason=f"project created, Slack channel not: {reason}"
        )
    queue_escalation_notice(rt, deal.id)


async def channel_attempts_so_far(rt: Runtime, deal_id: str) -> int:
    """How many channel attempts already failed retryably (moves into PARTIAL)."""
    async with rt.db.session() as session:
        rows = await session.exec(
            select(AuditLog).where(AuditLog.deal_id == deal_id, AuditLog.action == "transition")
        )
        return sum(1 for r in rows if (r.input_json or {}).get("to") == DealStatus.PARTIAL.value)
