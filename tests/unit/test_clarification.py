from __future__ import annotations

from app.services.clarification import compose_clarification
from app.services.intake_validation import FieldIssue, Problem

ISSUES = [
    FieldIssue("contact_email", Problem.MISSING, "not found in the email"),
    FieldIssue("sf_opportunity_url", Problem.INVALID, "'sf/acme' is not a Salesforce link"),
    FieldIssue("contact_name", Problem.UNGROUNDED, "the extracted value isn't in the AE's email"),
]


def test_lists_every_issue_in_plain_words() -> None:
    message = compose_clarification(
        ae_name="Ravi Kumar",
        customer_company="Acme Corp",
        original_subject="Closed: Acme",
        issues=ISSUES,
    )
    assert message.subject == "Closed: Acme"
    assert message.body.startswith("Hi Ravi,")
    assert "the Acme Corp handoff" in message.body
    assert "• Customer contact's email address\n" in message.body
    assert "• Salesforce opportunity link: 'sf/acme' is not a Salesforce link" in message.body
    # Internal wording ("ungrounded", "extracted value") never reaches the AE.
    assert "ungrounded" not in message.body
    assert "extracted" not in message.body
    assert "• Customer contact's name" in message.body


def test_reminder_wording_and_fallbacks() -> None:
    message = compose_clarification(
        ae_name=None, customer_company=None, original_subject="", issues=ISSUES[:1], reminder=True
    )
    assert message.body.startswith("Hi,\n\nA quick reminder")
    assert "your deal handoff" in message.body
    assert message.subject == "Deal handoff"
