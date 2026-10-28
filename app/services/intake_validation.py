"""Validate what the Intake Agent read. Pure: no I/O, no LLM.

The agent proposes; this module decides. For every field:

1. Grounding (G4): the value and its evidence quote must both be in the AE's own words,
   otherwise the field counts as missing. A hallucinated value can't get through.
2. Format: email syntax (and not the AE's own address), an HTTPS Salesforce link, and an
   international (E.164) phone number. Nothing is guessed: "98765 43210" without a
   country code is rejected, not completed.
3. Required (G1): `INTAKE_REQUIRED_FIELDS` from config.

Trusted sources win over the email. The AE's phone comes from the AE directory when the
sender is listed, and the AE's email is always the sender address.

What grounding does not do: it can't tell malicious text the sender really wrote from
genuine deal data. Authorship is the sender allowlist's job (G6).
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from urllib.parse import urlparse

from pydantic import EmailStr, TypeAdapter, ValidationError

from app.config import E164_PATTERN, AccountExecutive
from app.domain import INTAKE_FIELDS
from app.ports import DealIntake, ExtractedField
from app.services.grounding import is_grounded, normalise

MAX_TEXT_CHARS: Final = 200
SALESFORCE_HOSTS: Final = ("salesforce.com", "force.com")
_PHONE_SEPARATORS = re.compile(r"[\s\-().]")
_E164 = re.compile(E164_PATTERN)
_EMAIL: TypeAdapter[str] = TypeAdapter(EmailStr)

# What to tell the AE for each field, in the clarification email.
FIELD_LABELS: Final = {
    "customer_company": "Customer company name",
    "contact_name": "Customer contact's name",
    "contact_email": "Customer contact's email address",
    "ae_name": "Your name",
    "ae_phone": "Your phone number, with country code (e.g. +91 98765 43210)",
    "sf_opportunity_url": "Salesforce opportunity link",
}


class Problem(StrEnum):
    MISSING = "missing"
    UNGROUNDED = "ungrounded"  # the agent returned a value we can't find in the email
    INVALID = "invalid"


@dataclass(frozen=True)
class FieldIssue:
    field: str
    problem: Problem
    detail: str


@dataclass(frozen=True)
class IntakeResult:
    values: dict[str, str]
    issues: list[FieldIssue] = field(default_factory=list[FieldIssue])
    sources: dict[str, str] = field(default_factory=dict[str, str])  # field -> where it came from

    @property
    def complete(self) -> bool:
        return not self.issues


def validate_intake(
    intake: DealIntake,
    *,
    thread_text: str,
    sender_email: str,
    sender_name: str | None,
    directory_entry: AccountExecutive | None,
    required: Collection[str],
) -> IntakeResult:
    values: dict[str, str] = {"ae_email": sender_email.lower()}
    sources: dict[str, str] = {"ae_email": "sender address"}
    issues: list[FieldIssue] = []

    for name in INTAKE_FIELDS:
        value, source, issue = _resolve(
            name, getattr(intake, name), thread_text, sender_email, sender_name, directory_entry
        )
        if value is not None:
            values[name], sources[name] = value, source
        elif issue is not None and (name in required or issue.problem is Problem.INVALID):
            # An invalid optional value is still reported: the AE gave something wrong.
            issues.append(issue)
    return IntakeResult(values=values, issues=issues, sources=sources)


def _resolve(
    name: str,
    extracted: ExtractedField,
    thread_text: str,
    sender_email: str,
    sender_name: str | None,
    directory_entry: AccountExecutive | None,
) -> tuple[str | None, str, FieldIssue | None]:
    if name == "ae_phone" and directory_entry is not None:
        return directory_entry.phone, "AE directory", None

    grounded = _grounded_value(extracted, thread_text)
    if grounded is None:
        if name == "ae_name" and sender_name and sender_name.strip():
            return sender_name.strip()[:MAX_TEXT_CHARS], "sender display name", None
        problem = Problem.MISSING if extracted.value is None else Problem.UNGROUNDED
        detail = (
            "not found in the email"
            if problem is Problem.MISSING
            else ("the extracted value isn't in the AE's email")
        )
        return None, "", FieldIssue(name, problem, detail)

    checked, error = _check_format(name, grounded, sender_email)
    if checked is None:
        return None, "", FieldIssue(name, Problem.INVALID, error)
    return checked, "email (grounded)", None


def _grounded_value(extracted: ExtractedField, thread_text: str) -> str | None:
    value, evidence = (extracted.value or "").strip(), (extracted.evidence or "").strip()
    if not value or not evidence:
        return None
    # The quote must be in the email, and the value must be part of the quote.
    if not is_grounded(evidence, thread_text) or normalise(value) not in normalise(evidence):
        return None
    return value


def _check_format(name: str, value: str, sender_email: str) -> tuple[str | None, str]:
    match name:
        case "contact_email":
            try:
                email = _EMAIL.validate_python(value).lower()
            except ValidationError:
                return None, f"{value!r} is not a valid email address"
            if email == sender_email.lower():
                return None, "that's the AE's own address, not the customer contact's"
            return email, ""
        case "ae_phone":
            phone = _PHONE_SEPARATORS.sub("", value)
            if not _E164.fullmatch(phone):
                return None, f"{value!r} needs the country code, e.g. +91 98765 43210"
            return phone, ""
        case "sf_opportunity_url":
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            if parsed.scheme != "https" or not host.endswith(SALESFORCE_HOSTS):
                return None, f"{value!r} is not a Salesforce link"
            return value, ""
        case _:
            text = " ".join(value.split())
            if len(text) > MAX_TEXT_CHARS:
                return None, f"longer than {MAX_TEXT_CHARS} characters"
            return text, ""
