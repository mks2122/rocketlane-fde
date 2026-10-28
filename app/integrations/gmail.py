"""Gmail adapter for the CS inbox (implements `Mailbox`).

Auth: an OAuth refresh token from `scripts/gmail_auth.py`, stored in
`GMAIL_TOKEN_PATH`. Before every call the access token is refreshed if it has
expired, and any new token is written back atomically. A revoked or expired refresh
token (Google expires them after 7 days for apps in "Testing") raises
`GmailAuthError`. The poller turns that into a degraded `/health` and stops polling;
it is never retried.

The Google client is synchronous and built on httplib2, which is not thread-safe, so
every call runs in a worker thread behind one lock. Inbox volume is a few emails a
day, so serialising is free.

Processed messages get a label and are excluded by the list query. The database (the
unique Gmail message id) remains the real idempotency guard: if labelling fails, the
next poll sees the message again and `handle_inbound` treats it as a duplicate.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Final, cast

import google_auth_httplib2  # pyright: ignore[reportMissingTypeStubs]
import httplib2  # pyright: ignore[reportMissingTypeStubs]
import structlog
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import (  # pyright: ignore[reportMissingTypeStubs]
    build,  # pyright: ignore[reportUnknownVariableType]
)
from googleapiclient.errors import HttpError  # pyright: ignore[reportMissingTypeStubs]
from pydantic import ValidationError

from app.config import Settings
from app.errors import (
    AuthorizationError,
    IntegrationError,
    OutcomeUnknownError,
    PermanentError,
    RetryableError,
    ValidationFailedError,
)
from app.integrations.http import RETRYABLE_STATUS, parse_retry_after
from app.integrations.retry import RetryPolicy, Sleep, run_with_retries
from app.ports import InboundEmail, OutboundEmail

log = structlog.get_logger(__name__)

SCOPES: Final = ("https://www.googleapis.com/auth/gmail.modify",)
SERVICE: Final = "gmail"
MAX_LIST_PAGES: Final = 5  # 500 messages per poll is far beyond any real deal volume
PAGE_SIZE: Final = 100
# Gmail reports per-user rate limiting as 403 with these reasons, not 429.
_RATE_LIMIT_REASONS: Final = (b"rateLimitExceeded", b"userRateLimitExceeded")

# googleapiclient builds its resource objects dynamically from the discovery document
# and ships no type information, so the service handle is necessarily untyped.
GmailApi = Any
ServiceFactory = Callable[[Credentials], GmailApi]


class GmailAuthError(AuthorizationError):
    """The stored Gmail authorisation is missing, invalid, revoked or expired."""


# google-auth annotates Credentials only partially. Reading it through these helpers
# keeps the untyped surface in one place.
def _token(creds: Credentials) -> str | None:
    return cast("str | None", creds.token)  # pyright: ignore[reportUnknownMemberType]


def _has_refresh_token(creds: Credentials) -> bool:
    return bool(cast("str | None", creds.refresh_token))  # pyright: ignore[reportUnknownMemberType]


def _to_json(creds: Credentials) -> str:
    return creds.to_json()  # pyright: ignore[reportUnknownMemberType]


def _refresh(creds: Credentials, request: Any) -> None:
    creds.refresh(request)  # pyright: ignore[reportUnknownMemberType]


# --- token storage ----------------------------------------------------------------


class TokenStore:
    """The OAuth token file. Writes are atomic so a crash can't leave half a token."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> Credentials:
        if not self.path.is_file():
            raise GmailAuthError(
                f"no Gmail token at {self.path}; run `uv run python scripts/gmail_auth.py`"
            )
        try:
            creds = Credentials.from_authorized_user_file(  # pyright: ignore[reportUnknownMemberType]
                str(self.path), list(SCOPES)
            )
        except (ValueError, json.JSONDecodeError) as exc:
            raise GmailAuthError(f"Gmail token at {self.path} is invalid: {exc}") from exc
        if not _has_refresh_token(creds):
            raise GmailAuthError("Gmail token has no refresh token; re-run gmail_auth.py")
        return creds

    def save(self, creds: Credentials) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".token-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_to_json(creds))
            os.chmod(tmp, 0o600)  # best effort; a no-op beyond read-only on Windows
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# --- pure message helpers ---------------------------------------------------------


class _HtmlText(HTMLParser):
    _BREAKS: Final = frozenset({"br", "p", "div", "li", "tr", "h1", "h2", "h3"})
    _SKIP: Final = frozenset({"script", "style", "head"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HtmlText()
    parser.feed(html)
    lines = (" ".join(line.split()) for line in "".join(parser.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _headers(part: Mapping[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for header in cast(list[dict[str, str]], part.get("headers") or []):
        found.setdefault(header["name"].lower(), header["value"])
    return found


def _charset(part: Mapping[str, Any]) -> str:
    content_type = _headers(part).get("content-type", "")
    for param in content_type.split(";")[1:]:
        key, _, value = param.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip('"')
    return "utf-8"


def _decode(part: Mapping[str, Any]) -> str:
    body = cast(Mapping[str, str], part.get("body") or {})
    data = body.get("data") or ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return ""
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:  # unknown charset name
        return raw.decode("utf-8", errors="replace")


def _is_attachment(part: Mapping[str, Any]) -> bool:
    disposition = _headers(part).get("content-disposition", "").lower()
    return bool(part.get("filename")) or disposition.startswith("attachment")


def _walk(part: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    children = cast(list[Mapping[str, Any]], part.get("parts") or [])
    return [part, *(p for child in children for p in _walk(child))]


def extract_body(payload: Mapping[str, Any]) -> str:
    """Plain text of the message: the first text/plain part, else the first text/html."""
    parts = [p for p in _walk(payload) if not _is_attachment(p)]
    for mime, convert in (("text/plain", str), ("text/html", html_to_text)):
        for part in parts:
            if part.get("mimeType") == mime:
                text = _decode(part)
                if text.strip():
                    return convert(text).strip()
    return ""


def parse_message(raw: Mapping[str, Any], *, max_body_chars: int) -> InboundEmail:
    """Gmail `users.messages.get(format=full)` resource → InboundEmail.

    Raises ValidationFailedError for anything we can't trust (no usable sender, bad
    timestamp). The poller treats that as a poison message: skip it and label it.
    """
    payload = cast(Mapping[str, Any], raw.get("payload") or {})
    headers = _headers(payload)
    from_name, from_email = parseaddr(headers.get("from", ""))
    try:
        received_at = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, UTC)
        return InboundEmail(
            message_id=raw["id"],
            thread_id=raw["threadId"],
            rfc822_message_id=headers.get("message-id"),
            from_email=from_email,
            from_name=from_name or None,
            subject=headers.get("subject", ""),
            body_text=extract_body(payload)[:max_body_chars],
            received_at=received_at,
            from_self="SENT" in cast(list[str], raw.get("labelIds") or []),
        )
    except (KeyError, ValueError, ValidationError) as exc:
        # pydantic's ValidationError is a ValueError; keep both for readability.
        raise ValidationFailedError(
            f"unusable Gmail message {raw.get('id')}: {type(exc).__name__}"
        ) from exc


def build_raw_message(email: OutboundEmail) -> dict[str, str]:
    """OutboundEmail → `users.messages.send` body. Gmail fills in From (the account)."""
    msg = EmailMessage()
    msg["To"] = ", ".join(email.to)
    subject = email.subject
    if email.in_reply_to:
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        msg["In-Reply-To"] = email.in_reply_to
        msg["References"] = email.in_reply_to
    msg["Subject"] = subject
    msg.set_content(email.body_text)
    body = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")}
    if email.thread_id:
        body["threadId"] = email.thread_id
    return body


def classify_error(exc: Exception, *, idempotent: bool) -> IntegrationError:
    """Map a Google client / transport exception to a domain error."""
    if isinstance(exc, RefreshError):
        return GmailAuthError(f"Gmail authorisation rejected: {exc}")
    if isinstance(exc, HttpError):
        status = int(exc.resp.status)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        content: bytes = exc.content or b""  # pyright: ignore[reportUnknownMemberType]
        message = f"{SERVICE}: HTTP {status}"
        refused = status == 429 or (
            status == 403 and any(r in content for r in _RATE_LIMIT_REASONS)
        )
        if refused:
            retry_after = parse_retry_after(exc.resp.get("retry-after"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            return RetryableError(f"{message} (rate limited)", retry_after=retry_after)
        if status in RETRYABLE_STATUS:
            return RetryableError(message) if idempotent else OutcomeUnknownError(message)
        return PermanentError(f"{message}: {content[:300].decode('utf-8', errors='replace')}")
    # Network-level failure (timeout, reset, DNS, token endpoint unreachable).
    message = f"{SERVICE}: {type(exc).__name__}"
    return RetryableError(message) if idempotent else OutcomeUnknownError(message)


_TRANSIENT: Final = (TransportError, TimeoutError, OSError, httplib2.HttpLib2Error)


# --- adapter ----------------------------------------------------------------------


def default_service_factory(timeout_seconds: float) -> ServiceFactory:
    def factory(creds: Credentials) -> GmailApi:
        http = google_auth_httplib2.AuthorizedHttp(  # pyright: ignore[reportUnknownMemberType]
            creds, http=httplib2.Http(timeout=timeout_seconds)
        )
        return build("gmail", "v1", http=http, cache_discovery=False)  # pyright: ignore[reportUnknownVariableType]

    return factory


class GmailMailbox:
    def __init__(
        self,
        settings: Settings,
        *,
        token_store: TokenStore | None = None,
        service_factory: ServiceFactory | None = None,
        refresh_request: Callable[[], Any] | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        timeout = settings.http_read_timeout_seconds
        self._settings = settings
        self._store = token_store or TokenStore(settings.gmail_token_path)
        self._factory = service_factory or default_service_factory(timeout)
        self._refresh_request = refresh_request or (
            lambda: google_auth_httplib2.Request(httplib2.Http(timeout=timeout))  # pyright: ignore[reportUnknownMemberType, reportUnknownLambdaType]
        )
        self._policy = RetryPolicy.from_settings(settings)
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._creds: Credentials | None = None
        self._persisted_token: str | None = None
        self._service: GmailApi = None
        self._label_id: str | None = None

    # -- Mailbox port --

    async def list_new_message_ids(self) -> list[str]:
        query = (
            f"in:inbox -from:me -label:{self._settings.gmail_processed_label} "
            f"newer_than:{self._settings.gmail_lookback_days}d"
        )

        def run(api: GmailApi) -> list[str]:
            ids: list[str] = []
            token: str | None = None
            for _ in range(MAX_LIST_PAGES):
                page = (
                    api.users()
                    .messages()
                    .list(userId="me", q=query, maxResults=PAGE_SIZE, pageToken=token)
                    .execute()
                )
                ids.extend(m["id"] for m in page.get("messages", []))
                token = page.get("nextPageToken")
                if not token:
                    break
            else:
                log.warning("gmail_list_truncated", pages=MAX_LIST_PAGES, returned=len(ids))
            ids.reverse()  # Gmail lists newest first; process oldest first
            return ids

        return await self._call("list", run)

    async def fetch(self, message_id: str) -> InboundEmail:
        def run(api: GmailApi) -> dict[str, Any]:
            return api.users().messages().get(userId="me", id=message_id, format="full").execute()

        raw = await self._call("fetch", run)
        return parse_message(raw, max_body_chars=self._settings.gmail_max_body_chars)

    async def fetch_thread(self, thread_id: str) -> list[InboundEmail]:
        def run(api: GmailApi) -> dict[str, Any]:
            return api.users().threads().get(userId="me", id=thread_id, format="full").execute()

        raw = await self._call("fetch_thread", run)
        emails: list[InboundEmail] = []
        for message in cast(list[dict[str, Any]], raw.get("messages") or []):
            try:
                emails.append(
                    parse_message(message, max_body_chars=self._settings.gmail_max_body_chars)
                )
            except ValidationFailedError as exc:
                # One unreadable message (e.g. a bounce) shouldn't hide the rest of the thread.
                log.warning("gmail_thread_message_skipped", thread_id=thread_id, error=str(exc))
        return sorted(emails, key=lambda e: e.received_at)

    async def mark_processed(self, message_id: str) -> None:
        def run(api: GmailApi) -> None:
            label_id = self._ensure_label(api)
            api.users().messages().modify(
                userId="me", id=message_id, body={"addLabelIds": [label_id]}
            ).execute()

        await self._call("mark_processed", run)

    async def send(self, email: OutboundEmail) -> str:
        body = build_raw_message(email)

        def run(api: GmailApi) -> str:
            return api.users().messages().send(userId="me", body=body).execute()["id"]

        # Not idempotent: an ambiguous failure must not send the email twice.
        return await self._call("send", run, idempotent=False)

    async def account_email(self) -> str:
        def run(api: GmailApi) -> str:
            return api.users().getProfile(userId="me").execute()["emailAddress"]

        return await self._call("profile", run)

    # -- plumbing --

    async def _call[T](
        self, operation: str, fn: Callable[[GmailApi], T], *, idempotent: bool = True
    ) -> T:
        async def once() -> T:
            async with self._lock:
                return await asyncio.to_thread(self._run_sync, fn, idempotent)

        log.debug("gmail_call", operation=operation)
        return await run_with_retries(once, service=SERVICE, policy=self._policy, sleep=self._sleep)

    def _run_sync[T](self, fn: Callable[[GmailApi], T], idempotent: bool) -> T:
        try:
            self._ensure_fresh_token()
            return fn(self._api())
        except IntegrationError:
            raise
        except (RefreshError, HttpError, *_TRANSIENT) as exc:
            raise classify_error(exc, idempotent=idempotent) from exc
        finally:
            # The authorised transport may also refresh on a 401; persist whatever is newest.
            self._persist_if_changed()

    def _ensure_fresh_token(self) -> None:
        if self._creds is None:
            self._creds = self._store.load()
            self._persisted_token = _token(self._creds)
        if self._creds.valid:
            return
        try:
            _refresh(self._creds, self._refresh_request())
        except RefreshError as exc:
            raise GmailAuthError(f"Gmail token refresh rejected: {exc}") from exc
        log.info("gmail_token_refreshed")

    def _persist_if_changed(self) -> None:
        creds = self._creds
        if creds is None or _token(creds) == self._persisted_token:
            return
        try:
            self._store.save(creds)
            self._persisted_token = _token(creds)
        except OSError:
            # Not fatal: the refresh token is unchanged, so the next start refreshes again.
            log.exception("gmail_token_persist_failed", path=str(self._store.path))

    def _api(self) -> GmailApi:
        if self._service is None:
            assert self._creds is not None  # noqa: S101  # set by _ensure_fresh_token
            self._service = self._factory(self._creds)
        return self._service

    def _ensure_label(self, api: GmailApi) -> str:
        if self._label_id is not None:
            return self._label_id
        name = self._settings.gmail_processed_label
        labels = api.users().labels().list(userId="me").execute().get("labels", [])
        existing = next((lb["id"] for lb in labels if lb["name"] == name), None)
        if existing is None:
            created = (
                api.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )
            existing = created["id"]
            log.info("gmail_label_created", label=name)
        self._label_id = cast(str, existing)
        return self._label_id
