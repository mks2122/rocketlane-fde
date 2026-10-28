"""Builders for test data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.ports import InboundEmail

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)
AE_EMAIL = "ravi.ae@novacrm-demo.com"


def inbound_email(
    message_id: str = "m1",
    *,
    thread_id: str | None = None,
    sender: str = AE_EMAIL,
    minutes: int = 0,
    body: str = "Customer: Acme Corp",
) -> InboundEmail:
    return InboundEmail(
        message_id=message_id,
        thread_id=thread_id or f"t-{message_id}",
        rfc822_message_id=f"<{message_id}@mail.example.com>",
        from_email=sender,
        from_name="Ravi Kumar",
        subject="New deal: Acme Corp",
        body_text=body,
        received_at=T0 + timedelta(minutes=minutes),
    )
