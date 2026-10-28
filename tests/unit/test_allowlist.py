from __future__ import annotations

import pytest

from app.services.allowlist import is_sender_allowed

DOMAINS = ["novacrm.com"]
EMAILS = ["karthick.ae@gmail.com"]


@pytest.mark.parametrize(
    ("sender", "allowed"),
    [
        ("ravi@novacrm.com", True),
        ("  Ravi@NovaCRM.com ", True),
        ("karthick.ae@gmail.com", True),
        ("someone.else@gmail.com", False),  # allowed address doesn't open its whole domain
        ("ravi@mail.novacrm.com", False),  # subdomains must be listed explicitly
        ("ravi@novacrm.com.evil.io", False),
        ("ravi@evilnovacrm.com", False),
        ("novacrm.com", False),
        ("@novacrm.com", False),
        ("", False),
    ],
)
def test_allowlist(sender: str, allowed: bool) -> None:
    assert is_sender_allowed(sender, allowed_domains=DOMAINS, allowed_emails=EMAILS) is allowed


def test_fails_closed_when_nothing_is_configured() -> None:
    assert not is_sender_allowed("ravi@novacrm.com", allowed_domains=[], allowed_emails=[])
