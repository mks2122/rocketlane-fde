"""Sender allowlist (guardrail G6). Pure: no I/O."""

from __future__ import annotations

from collections.abc import Iterable


def is_sender_allowed(
    sender: str, *, allowed_domains: Iterable[str], allowed_emails: Iterable[str]
) -> bool:
    """Exact address or exact domain match, case-insensitive. Fails closed when both are empty.

    Subdomains are deliberately not matched: `mail.novacrm.com` must be listed itself,
    so a look-alike subdomain can't slip in.
    """
    address = sender.strip().lower()
    local, at, domain = address.rpartition("@")
    if not at or not local or not domain:
        return False
    if address in {e.strip().lower() for e in allowed_emails}:
        return True
    return domain in {d.strip().lower() for d in allowed_domains}
