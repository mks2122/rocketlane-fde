"""Clarification emails to the AE. Pure: a fixed template, not LLM text.

Asking for exactly the missing or invalid fields, in plain words, is a deterministic job.
A template keeps it predictable and testable, and stops any email content from echoing
back into what we send.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.services.intake_validation import FIELD_LABELS, FieldIssue, Problem


@dataclass(frozen=True)
class Clarification:
    subject: str
    body: str


def _line(issue: FieldIssue) -> str:
    label = FIELD_LABELS.get(issue.field, issue.field)
    if issue.problem is Problem.INVALID:
        return f"• {label}: {issue.detail}"
    return f"• {label}"


def compose_clarification(
    *,
    ae_name: str | None,
    customer_company: str | None,
    original_subject: str,
    issues: Sequence[FieldIssue],
    reminder: bool = False,
) -> Clarification:
    greeting = f"Hi {ae_name.split()[0]}," if ae_name and ae_name.split() else "Hi,"
    deal = f"the {customer_company} handoff" if customer_company else "your deal handoff"
    opener = (
        f"A quick reminder: onboarding for {deal} is still waiting on a few details."
        if reminder
        else f"Thanks for {deal}. Before onboarding can start, we need a few more details:"
    )
    body = (
        f"{greeting}\n\n{opener}\n\n"
        + "\n".join(_line(i) for i in issues)
        + "\n\nJust reply to this email with them. No need to resend anything else.\n\n"
        "NovaCRM Onboarding (automated message)"
    )
    return Clarification(subject=original_subject or "Deal handoff", body=body)
