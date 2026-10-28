"""Escalation notice text. Pure: the same message goes to Slack and email.

A notice answers three questions for whoever picks it up: what happened, what the
system already tried, and how to resolve it. It carries ids, names and masked phone
numbers, never email bodies or transcripts (recording links are internal).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.models import CallAttempt, Deal


@dataclass(frozen=True)
class EscalationNotice:
    subject: str
    text: str


def mask_phone(number: str | None) -> str:
    if not number:
        return "unknown"
    return f"{number[:3]}…{number[-4:]}"


def _attempt_line(attempt: CallAttempt) -> str:
    when = attempt.started_at.strftime("%Y-%m-%d %H:%M UTC")
    outcome = attempt.outcome or "in progress"
    line = f"• Call {attempt.attempt_no} ({when}): {outcome}"
    if attempt.outcome_reason:
        line += f": {attempt.outcome_reason}"
    if attempt.recording_url:
        line += f"\n  recording: {attempt.recording_url}"
    return line


def compose_notice(deal: Deal, *, reason: str, attempts: Sequence[CallAttempt]) -> EscalationNotice:
    customer = deal.customer_company or "unknown customer"
    tried = "\n".join(_attempt_line(a) for a in attempts) or "• No calls were placed."
    ae = deal.ae_name or "unknown AE"
    if deal.ae_email:
        ae += f" <{deal.ae_email}>"
    text = (
        f"🚨 Onboarding escalation: {customer}\n"
        f"Deal {deal.id} needs a person.\n\n"
        f"What happened\n{reason}\n\n"
        f"What was tried\n{tried}\n\n"
        f"Deal\n"
        f"• AE: {ae}, phone {mask_phone(deal.ae_phone)}\n"
        f"• Salesforce: {deal.sf_opportunity_url or 'not provided'}\n"
        f"• Plan tier: {deal.plan_tier or 'not confirmed'}\n\n"
        f"How to resolve\n"
        f"Confirm the missing information with the AE, then resolve the deal through the admin "
        f"API: POST /deals/{deal.id}/resolve (actor and reason are required and audited)."
    )
    return EscalationNotice(
        subject=f"[NovaCRM onboarding] Escalation: {customer} ({deal.id})", text=text
    )
