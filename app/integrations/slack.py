"""Slack adapter (implements `ChatWorkspace`) on the Web API via `HttpService`.

Why not slack_sdk: its async client needs aiohttp. Going through HttpService gives Slack
the same timeouts, Retry-After handling, concurrency cap and create-safety as every
other integration.

Slack reports most failures as HTTP 200 with `{"ok": false, "error": "<code>"}`.
`_raise_for_slack_error` maps those codes to domain errors. HTTP 429 (rate limit) is
already handled by HttpService.

Bot token scopes used: channels:read, channels:manage, chat:write, users:read,
users:read.email. The bot must be a member of a channel to post in it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, cast

import httpx
import structlog

from app.config import Settings
from app.errors import AuthorizationError, OutcomeUnknownError, PermanentError, RetryableError
from app.integrations.http import HttpService, ResponseCheck
from app.ports import ChannelRef

log = structlog.get_logger(__name__)

SERVICE: Final = "slack"
MAX_LIST_PAGES: Final = 20
PAGE_SIZE: Final = 200
_AUTH_ERRORS: Final = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "token_revoked",
        "token_expired",
        "account_inactive",
        "missing_scope",
    }
)
_TRANSIENT_ERRORS: Final = frozenset(
    {"internal_error", "fatal_error", "service_unavailable", "request_timeout", "ratelimited"}
)
# Invite outcomes that already mean "this person is in the channel".
_ALREADY_MEMBER: Final = frozenset({"already_in_channel", "cant_invite_self"})
_HINTS: Final = {
    "not_in_channel": "invite the bot to the channel first (/invite @bot)",
    "channel_not_found": "check the channel name and that the bot can see it",
    "missing_scope": "add the missing scope to the Slack app and reinstall it",
}


def _raise_for_slack_error(method: str, body: Mapping[str, Any], *, idempotent: bool) -> None:
    if body.get("ok") is True:
        return
    code = str(body.get("error") or "unknown_error")
    hint = _HINTS.get(code)
    message = f"{SERVICE} {method}: {code}" + (f" ({hint})" if hint else "")
    if code in _AUTH_ERRORS:
        raise AuthorizationError(message)
    if code in _TRANSIENT_ERRORS:
        raise RetryableError(message) if idempotent else OutcomeUnknownError(message)
    raise PermanentError(message)


class SlackWorkspace:
    def __init__(self, settings: Settings, *, http: HttpService | None = None) -> None:
        settings.require("slack_bot_token")
        assert settings.slack_bot_token is not None  # noqa: S101  # checked by require()
        self._connect_enabled = settings.slack_connect_enabled
        self._http = http or HttpService.create(
            settings,
            name=SERVICE,
            base_url=settings.slack_base_url,
            headers={"Authorization": f"Bearer {settings.slack_bot_token.get_secret_value()}"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # -- ChatWorkspace port --

    async def find_channel(self, name: str) -> ChannelRef | None:
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            params = {"types": "public_channel", "exclude_archived": "true", "limit": PAGE_SIZE}
            if cursor:
                params["cursor"] = cursor
            body = await self._get("conversations.list", params)
            for channel in cast(list[dict[str, Any]], body.get("channels") or []):
                if channel.get("name") == name:
                    return ChannelRef(channel_id=channel["id"], name=name)
            cursor = cast(dict[str, Any], body.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return None
        log.warning("slack_channel_list_truncated", pages=MAX_LIST_PAGES)
        return None

    async def create_channel(self, name: str) -> ChannelRef:
        body = await self._post(
            "conversations.create", {"name": name, "is_private": False}, idempotent=False
        )
        channel = cast(dict[str, Any], body["channel"])
        return ChannelRef(channel_id=channel["id"], name=channel["name"])

    async def set_topic(self, channel_id: str, topic: str) -> None:
        await self._post("conversations.setTopic", {"channel": channel_id, "topic": topic})

    async def post_message(self, channel_id: str, text: str) -> str:
        body = await self._post(
            "chat.postMessage",
            {"channel": channel_id, "text": text, "unfurl_links": False, "unfurl_media": False},
            idempotent=False,
        )
        return str(body["ts"])

    async def invite_members(self, channel_id: str, emails: Sequence[str]) -> None:
        # One user per call: Slack fails a whole batch if any one user is already a member.
        for email in emails:
            user = await self._get("users.lookupByEmail", {"email": email})
            user_id = cast(dict[str, Any], user["user"])["id"]
            try:
                await self._post("conversations.invite", {"channel": channel_id, "users": user_id})
            except PermanentError as exc:
                if not any(code in str(exc) for code in _ALREADY_MEMBER):
                    raise

    async def invite_external(self, channel_id: str, email: str) -> None:
        if not self._connect_enabled:
            # Slack Connect needs a paid workspace. The demo logs what would be sent.
            log.info("slack_connect_invite_mocked", channel_id=channel_id, email=email)
            return
        await self._post("conversations.inviteShared", {"channel": channel_id, "emails": email})

    # -- plumbing --

    async def _get(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        response = await self._http.request(
            "GET", f"/{method}", params=dict(params), check=_checker(method, idempotent=True)
        )
        return _body(response)

    async def _post(
        self, method: str, payload: Mapping[str, Any], *, idempotent: bool = True
    ) -> dict[str, Any]:
        response = await self._http.request(
            "POST",
            f"/{method}",
            json=dict(payload),
            idempotent=idempotent,
            check=_checker(method, idempotent=idempotent),
        )
        return _body(response)


def _body(response: httpx.Response) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], response.json())
    except ValueError as exc:
        raise PermanentError(f"{SERVICE}: non-JSON response ({response.status_code})") from exc


def _checker(method: str, *, idempotent: bool) -> ResponseCheck:
    # Runs inside HttpService's retry loop, so transient ok:false errors are retried.
    def check(response: httpx.Response) -> None:
        _raise_for_slack_error(method, _body(response), idempotent=idempotent)

    return check
