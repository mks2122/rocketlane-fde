"""Intake validation: grounding, formats, required fields, trusted sources. Pure."""

from __future__ import annotations

from typing import Any

import pytest

from app.config import AccountExecutive
from app.domain import INTAKE_FIELDS
from app.ports import DealIntake, ExtractedField
from app.services.intake_validation import Problem, validate_intake

SF = "https://novacrm.lightning.force.com/lightning/r/Opportunity/006Hs00001AbCdE/view"
EMAIL = f"""From: Ravi Kumar <ravi.ae@novacrm-demo.com>
Subject: Closed: Acme Corp

Hi team, we just closed Acme Corp!
Main contact: Jane Doe (jane.doe@acme.com).
SF: {SF}
Reach me on +91 98765-43210.
Thanks, Ravi Kumar"""

GOOD: dict[str, tuple[str, str]] = {
    "customer_company": ("Acme Corp", "we just closed Acme Corp"),
    "contact_name": ("Jane Doe", "Main contact: Jane Doe"),
    "contact_email": ("Jane.Doe@acme.com", "Jane Doe (jane.doe@acme.com)"),
    "ae_name": ("Ravi Kumar", "Thanks, Ravi Kumar"),
    "sf_opportunity_url": (SF, f"SF: {SF}"),
}
DIRECTORY = AccountExecutive(
    name="Ravi Kumar", email="ravi.ae@novacrm-demo.com", phone="+919000000001"
)


def _intake(**overrides: tuple[str | None, str | None]) -> DealIntake:
    fields: dict[str, Any] = {
        name: ExtractedField(value=v, evidence=e) for name, (v, e) in {**GOOD, **overrides}.items()
    }
    return DealIntake(is_deal_handoff=True, notes=None, **fields)


def _validate(intake: DealIntake, **kwargs: Any) -> Any:
    args: dict[str, Any] = {
        "thread_text": EMAIL,
        "sender_email": "ravi.ae@novacrm-demo.com",
        "sender_name": "Ravi Kumar",
        "directory_entry": DIRECTORY,
        "required": INTAKE_FIELDS,
        **kwargs,
    }
    return validate_intake(intake, **args)


def _problems(result: Any) -> dict[str, Problem]:
    return {issue.field: issue.problem for issue in result.issues}


def test_complete_email_is_valid_and_normalised() -> None:
    result = _validate(_intake())
    assert result.complete
    assert result.values == {
        "customer_company": "Acme Corp",
        "contact_name": "Jane Doe",
        "contact_email": "jane.doe@acme.com",
        "ae_name": "Ravi Kumar",
        "ae_phone": "+919000000001",
        "sf_opportunity_url": SF,
        "ae_email": "ravi.ae@novacrm-demo.com",
    }


def test_missing_required_field() -> None:
    result = _validate(_intake(contact_email=(None, None)))
    assert _problems(result) == {"contact_email": Problem.MISSING}


def test_hallucinated_value_is_rejected() -> None:
    # The agent "found" a contact the AE never wrote.
    result = _validate(_intake(contact_name=("John Smith", "Main contact: John Smith")))
    assert _problems(result) == {"contact_name": Problem.UNGROUNDED}


def test_value_must_be_inside_its_quote() -> None:
    # Real quote, but the value isn't in it.
    result = _validate(_intake(customer_company=("Globex", "we just closed Acme Corp")))
    assert _problems(result) == {"customer_company": Problem.UNGROUNDED}


def test_value_without_evidence_is_rejected() -> None:
    result = _validate(_intake(customer_company=("Acme Corp", None)))
    assert _problems(result) == {"customer_company": Problem.UNGROUNDED}


@pytest.mark.parametrize(
    ("field", "value", "evidence"),
    [
        ("contact_email", "jane.doe@", "Jane Doe (jane.doe@"),
        (
            "contact_email",
            "ravi.ae@novacrm-demo.com",
            "From: Ravi Kumar <ravi.ae@novacrm-demo.com>",
        ),
        (
            "sf_opportunity_url",
            "novacrm.lightning.force.com",
            "https://novacrm.lightning.force.com",
        ),
    ],
    ids=["bad-email", "ae-as-contact", "not-https-salesforce"],
)
def test_invalid_formats(field: str, value: str, evidence: str) -> None:
    result = _validate(_intake(**{field: (value, evidence)}))
    assert _problems(result) == {field: Problem.INVALID}


def test_non_salesforce_link_is_rejected() -> None:
    email = EMAIL + "\nDeck: https://evil.example.com/opportunity"
    result = _validate(
        _intake(
            sf_opportunity_url=(
                "https://evil.example.com/opportunity",
                "Deck: https://evil.example.com/opportunity",
            )
        ),
        thread_text=email,
    )
    assert _problems(result) == {"sf_opportunity_url": Problem.INVALID}


def test_phone_comes_from_the_directory_never_the_email() -> None:
    # The email says +91 98765-43210; only the directory number may be called.
    result = _validate(_intake())
    assert result.values["ae_phone"] == "+919000000001"
    assert result.sources["ae_phone"] == "AE directory"


def test_ae_missing_from_directory_has_no_phone() -> None:
    result = _validate(_intake(), directory_entry=None)
    assert "ae_phone" not in result.values
    assert _problems(result) == {"ae_phone": Problem.NOT_IN_DIRECTORY}


def test_missing_directory_entry_reported_even_when_phone_is_optional() -> None:
    required = [f for f in INTAKE_FIELDS if f != "ae_phone"]
    result = _validate(_intake(), directory_entry=None, required=required)
    assert _problems(result) == {"ae_phone": Problem.NOT_IN_DIRECTORY}


def test_ae_name_falls_back_to_sender_display_name() -> None:
    result = _validate(_intake(ae_name=(None, None)), sender_name="Ravi K.")
    assert result.values["ae_name"] == "Ravi K."
    assert result.sources["ae_name"] == "sender display name"


def test_optional_fields_only_reported_when_invalid() -> None:
    required = [f for f in INTAKE_FIELDS if f != "sf_opportunity_url"]
    missing = _validate(_intake(sf_opportunity_url=(None, None)), required=required)
    invalid = _validate(
        _intake(sf_opportunity_url=("SF: link", "SF: link")),
        required=required,
        thread_text="SF: link",
    )
    assert missing.complete
    assert "sf_opportunity_url" in _problems(invalid)


def test_several_problems_are_all_reported() -> None:
    result = _validate(
        _intake(contact_name=(None, None), contact_email=("nope", "nope")),
        thread_text=EMAIL + "\nnope",
    )
    assert _problems(result) == {"contact_name": Problem.MISSING, "contact_email": Problem.INVALID}
