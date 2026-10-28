from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email import message_from_bytes
from email.message import Message
from pathlib import Path
from typing import Any

import httplib2  # pyright: ignore[reportMissingTypeStubs]
import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import (  # pyright: ignore[reportMissingTypeStubs]
    build,  # pyright: ignore[reportUnknownVariableType]
)
from googleapiclient.errors import HttpError  # pyright: ignore[reportMissingTypeStubs]
from googleapiclient.http import HttpMockSequence  # pyright: ignore[reportMissingTypeStubs]

from app.config import Settings
from app.errors import OutcomeUnknownError, PermanentError, RetryableError, ValidationFailedError
from app.integrations.gmail import (
    GmailAuthError,
    GmailMailbox,
    TokenStore,
    build_raw_message,
    classify_error,
    html_to_text,
    parse_message,
)
from app.ports import OutboundEmail

RECEIVED_AT = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)
INTERNAL_DATE = str(int(RECEIVED_AT.timestamp() * 1000))  # Gmail: epoch milliseconds


def _b64(text: str, encoding: str = "utf-8") -> str:
    return base64.urlsafe_b64encode(text.encode(encoding)).decode().rstrip("=")


def _part(
    mime: str, text: str, *, headers: Sequence[tuple[str, str]] = (), **extra: Any
) -> dict[str, Any]:
    return {
        "mimeType": mime,
        "headers": [{"name": n, "value": v} for n, v in headers],
        "body": {"data": _b64(text)},
        **extra,
    }


def _message(
    payload: dict[str, Any], *, sender: str = "Ravi Kumar <ravi.ae@example.com>"
) -> dict[str, Any]:
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": "New deal: Acme"},
        {"name": "Message-ID", "value": "<abc@mail.example.com>"},
    ]
    return {
        "id": "m1",
        "threadId": "t1",
        "internalDate": INTERNAL_DATE,
        "payload": {**payload, "headers": headers + payload.get("headers", [])},
    }


# --- parse_message ---------------------------------------------------------------


def test_parses_plain_text_message() -> None:
    email = parse_message(_message(_part("text/plain", "Customer: Acme")), max_body_chars=1000)
    assert email.message_id == "m1"
    assert email.thread_id == "t1"
    assert email.from_email == "ravi.ae@example.com"
    assert email.from_name == "Ravi Kumar"
    assert email.subject == "New deal: Acme"
    assert email.rfc822_message_id == "<abc@mail.example.com>"
    assert email.body_text == "Customer: Acme"
    assert email.received_at == RECEIVED_AT


def test_prefers_plain_over_html_and_skips_attachments() -> None:
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            _part("text/plain", "ATTACHMENT", filename="notes.txt"),
            {
                "mimeType": "multipart/alternative",
                "parts": [_part("text/html", "<p>html</p>"), _part("text/plain", "plain body")],
            },
        ],
    }
    assert parse_message(_message(payload), max_body_chars=1000).body_text == "plain body"


def test_falls_back_to_html_as_text() -> None:
    html = (
        "<html><head><style>p{}</style></head>"
        "<body><p>Customer:&nbsp;Acme</p><p>Plan TBD</p></body></html>"
    )
    body = parse_message(_message(_part("text/html", html)), max_body_chars=1000).body_text
    assert body == "Customer: Acme\nPlan TBD"


def test_respects_declared_charset() -> None:
    part = {
        "mimeType": "text/plain",
        "headers": [{"name": "Content-Type", "value": 'text/plain; charset="iso-8859-1"'}],
        "body": {"data": _b64("Café Ölz", "iso-8859-1")},
    }
    assert parse_message(_message(part), max_body_chars=1000).body_text == "Café Ölz"


def test_body_is_truncated() -> None:
    email = parse_message(_message(_part("text/plain", "x" * 5000)), max_body_chars=1000)
    assert len(email.body_text) == 1000


@pytest.mark.parametrize("sender", ["", "not an address", "Ravi <>"])
def test_unusable_sender_is_rejected(sender: str) -> None:
    with pytest.raises(ValidationFailedError, match="unusable Gmail message m1"):
        parse_message(_message(_part("text/plain", "x"), sender=sender), max_body_chars=1000)


def test_missing_internal_date_is_rejected() -> None:
    raw = _message(_part("text/plain", "x"))
    del raw["internalDate"]
    with pytest.raises(ValidationFailedError):
        parse_message(raw, max_body_chars=1000)


def test_html_to_text_drops_scripts() -> None:
    assert html_to_text("<div>a</div><script>alert(1)</script><div>b</div>") == "a\nb"


# --- build_raw_message -----------------------------------------------------------


def _decoded(body: dict[str, str]) -> Message:
    return message_from_bytes(base64.urlsafe_b64decode(body["raw"]))


def test_reply_is_threaded() -> None:
    body = build_raw_message(
        OutboundEmail(
            to=("ravi.ae@example.com",),
            subject="New deal: Acme",
            body_text="Which plan did Acme buy?",
            thread_id="t1",
            in_reply_to="<abc@mail.example.com>",
        )
    )
    msg = _decoded(body)
    assert body["threadId"] == "t1"
    assert msg["Subject"] == "Re: New deal: Acme"
    assert msg["In-Reply-To"] == "<abc@mail.example.com>"
    assert msg["References"] == "<abc@mail.example.com>"
    assert msg.get_payload(decode=True).decode().strip() == "Which plan did Acme buy?"  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue, reportOptionalMemberAccess]


def test_new_email_has_no_thread_headers() -> None:
    body = build_raw_message(
        OutboundEmail(to=("a@example.com", "b@example.com"), subject="Escalation", body_text="x")
    )
    msg = _decoded(body)
    assert "threadId" not in body
    assert msg["To"] == "a@example.com, b@example.com"
    assert msg["Subject"] == "Escalation"
    assert msg["In-Reply-To"] is None


# --- classify_error --------------------------------------------------------------


def _http_error(
    status: int, content: bytes = b"{}", headers: dict[str, str] | None = None
) -> HttpError:
    return HttpError(httplib2.Response({"status": status, **(headers or {})}), content)


def test_429_is_retryable_with_retry_after() -> None:
    err = classify_error(_http_error(429, headers={"retry-after": "3"}), idempotent=False)
    assert isinstance(err, RetryableError)
    assert not isinstance(err, OutcomeUnknownError)
    assert err.retry_after == 3.0


def test_403_rate_limit_is_retryable() -> None:
    content = b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}'
    assert type(classify_error(_http_error(403, content), idempotent=True)) is RetryableError


def test_403_forbidden_is_permanent() -> None:
    err = classify_error(_http_error(403, b"insufficientPermissions"), idempotent=True)
    assert isinstance(err, PermanentError)


def test_5xx_depends_on_idempotency() -> None:
    assert type(classify_error(_http_error(503), idempotent=True)) is RetryableError
    assert type(classify_error(_http_error(503), idempotent=False)) is OutcomeUnknownError


def test_network_errors_depend_on_idempotency() -> None:
    assert type(classify_error(TimeoutError(), idempotent=True)) is RetryableError
    assert type(classify_error(TimeoutError(), idempotent=False)) is OutcomeUnknownError


def test_refresh_error_is_auth_error() -> None:
    assert isinstance(
        classify_error(RefreshError("invalid_grant"), idempotent=True), GmailAuthError
    )


# --- TokenStore ------------------------------------------------------------------


def _creds(*, expired: bool = False, refresh_token: str | None = "refresh-1") -> Credentials:  # noqa: S107
    # google-auth compares expiry as naive UTC.
    expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=-1 if expired else 1)
    return Credentials(  # pyright: ignore[reportCallIssue]
        token="access-1",
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client",
        client_secret="secret",
        scopes=["https://www.googleapis.com/auth/gmail.modify"],
        expiry=expiry,
    )


def test_token_store_round_trip_is_atomic(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets" / "token.json")
    store.save(_creds())
    assert store.load().refresh_token == "refresh-1"  # pyright: ignore[reportUnknownMemberType]
    assert [p.name for p in store.path.parent.iterdir()] == ["token.json"]  # no temp files left


def test_token_store_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GmailAuthError, match=r"run `uv run python scripts/gmail_auth.py`"):
        TokenStore(tmp_path / "token.json").load()


def test_token_store_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(GmailAuthError, match="invalid"):
        TokenStore(path).load()


def test_token_without_refresh_token_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    data = json.loads(_creds().to_json())  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
    data["refresh_token"] = ""
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(GmailAuthError):
        TokenStore(path).load()


# --- GmailMailbox against the real client with a mocked transport ----------------


class _MemoryStore(TokenStore):
    def __init__(self, creds: Credentials) -> None:
        super().__init__(Path("unused"))
        self.creds = creds
        self.saved: list[str] = []

    def load(self) -> Credentials:
        return self.creds

    def save(self, creds: Credentials) -> None:
        self.saved.append(creds.token)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]


def _ok(body: object) -> tuple[dict[str, str], str]:
    return ({"status": "200"}, body if isinstance(body, str) else json.dumps(body))


def _mailbox(
    settings: Settings,
    responses: list[tuple[dict[str, str], str]],
    *,
    store: TokenStore | None = None,
    sleeps: list[float] | None = None,
) -> GmailMailbox:
    async def record_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    http = HttpMockSequence(responses)

    def service_factory(_creds: Credentials) -> Any:
        return build("gmail", "v1", http=http, cache_discovery=False)  # pyright: ignore[reportUnknownVariableType]

    return GmailMailbox(
        settings,
        token_store=store or _MemoryStore(_creds()),
        service_factory=service_factory,
        refresh_request=lambda: object(),
        sleep=record_sleep,
    )


async def test_list_pages_and_returns_oldest_first(settings: Settings) -> None:
    mailbox = _mailbox(
        settings,
        [
            _ok({"messages": [{"id": "m4"}, {"id": "m3"}], "nextPageToken": "p2"}),
            _ok({"messages": [{"id": "m2"}, {"id": "m1"}]}),
        ],
    )
    assert await mailbox.list_new_message_ids() == ["m1", "m2", "m3", "m4"]


async def test_list_retries_transient_errors(settings: Settings) -> None:
    sleeps: list[float] = []
    mailbox = _mailbox(
        settings,
        [({"status": "503"}, "{}"), _ok({"messages": [{"id": "m1"}]})],
        sleeps=sleeps,
    )
    assert await mailbox.list_new_message_ids() == ["m1"]
    assert len(sleeps) == 1


async def test_fetch_parses_message(settings: Settings) -> None:
    mailbox = _mailbox(settings, [_ok(_message(_part("text/plain", "Customer: Acme")))])
    email = await mailbox.fetch("m1")
    assert email.body_text == "Customer: Acme"


async def test_mark_processed_creates_label_once(settings: Settings) -> None:
    mailbox = _mailbox(
        settings,
        [
            _ok({"labels": [{"id": "INBOX", "name": "INBOX"}]}),
            _ok({"id": "Label_9", "name": settings.gmail_processed_label}),
            _ok({}),
            _ok({}),  # second message: label id is cached, only the modify call
        ],
    )
    await mailbox.mark_processed("m1")
    await mailbox.mark_processed("m2")


async def test_send_returns_id_and_is_not_retried_when_ambiguous(settings: Settings) -> None:
    mailbox = _mailbox(settings, [_ok({"id": "sent-1"}), ({"status": "500"}, "{}")])
    email = OutboundEmail(to=("a@example.com",), subject="s", body_text="b")
    assert await mailbox.send(email) == "sent-1"
    with pytest.raises(OutcomeUnknownError):
        await mailbox.send(email)


async def test_expired_token_is_refreshed_and_persisted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _MemoryStore(_creds(expired=True))

    def fake_refresh(self: Credentials, _request: object) -> None:
        self.token = "access-2"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    mailbox = _mailbox(settings, [_ok({"messages": []})], store=store)
    assert await mailbox.list_new_message_ids() == []
    assert store.saved == ["access-2"]


async def test_revoked_token_raises_auth_error_without_retry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def revoked(self: Credentials, _request: object) -> None:
        nonlocal calls
        calls += 1
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr(Credentials, "refresh", revoked)
    mailbox = _mailbox(settings, [], store=_MemoryStore(_creds(expired=True)))
    with pytest.raises(GmailAuthError, match="invalid_grant"):
        await mailbox.list_new_message_ids()
    assert calls == 1


# --- threads -----------------------------------------------------------------------


def test_sent_label_marks_our_own_messages() -> None:
    raw = _message(_part("text/plain", "Which plan?"))
    raw["labelIds"] = ["SENT"]
    assert parse_message(raw, max_body_chars=1000).from_self is True
    assert parse_message(_message(_part("text/plain", "x")), max_body_chars=1000).from_self is False


async def test_fetch_thread_orders_messages_and_skips_unreadable(settings: Settings) -> None:
    first = {**_message(_part("text/plain", "Deal: Acme")), "id": "m1"}
    reply = {
        **_message(_part("text/plain", "Contact is jane@acme.com")),
        "id": "m3",
        "internalDate": str(int(RECEIVED_AT.timestamp() * 1000) + 120_000),
    }
    ours = {
        **_message(_part("text/plain", "What is the contact email?")),
        "id": "m2",
        "labelIds": ["SENT"],
        "internalDate": str(int(RECEIVED_AT.timestamp() * 1000) + 60_000),
    }
    bounce: dict[str, Any] = {"id": "m4", "threadId": "t1", "payload": {}}  # no sender, no date
    mailbox = _mailbox(settings, [_ok({"id": "t1", "messages": [reply, bounce, first, ours]})])

    thread = await mailbox.fetch_thread("t1")

    assert [e.message_id for e in thread] == ["m1", "m2", "m3"]
    assert [e.from_self for e in thread] == [False, True, False]
