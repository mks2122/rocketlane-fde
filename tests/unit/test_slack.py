from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.errors import (
    AuthorizationError,
    ConfigError,
    OutcomeUnknownError,
    PermanentError,
    RetryableError,
)
from app.integrations.http import HttpService
from app.integrations.retry import RetryPolicy
from app.integrations.slack import SlackWorkspace

BASE = "https://slack.test/api"


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


def _workspace(settings: Settings, *, connect: bool = False) -> SlackWorkspace:
    async def no_sleep(_seconds: float) -> None:
        return None

    configured = settings.model_copy(
        update={"slack_bot_token": SecretStr("xoxb-test"), "slack_connect_enabled": connect}
    )
    http = HttpService(
        httpx.AsyncClient(base_url=BASE),
        name="slack",
        policy=RetryPolicy(attempts=3, base_seconds=0, max_seconds=0),
        max_concurrency=2,
        sleep=no_sleep,
    )
    return SlackWorkspace(configured, http=http)


@pytest.fixture
async def slack(settings: Settings) -> AsyncIterator[SlackWorkspace]:
    workspace = _workspace(settings)
    yield workspace
    await workspace.aclose()


def _requests(route: respx.Route) -> list[httpx.Request]:
    # respx types `calls` items loosely; the requests themselves are plain httpx.Request.
    return [call.request for call in route.calls]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def _ok(**fields: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, **fields})


def _err(code: str) -> httpx.Response:
    return httpx.Response(200, json={"ok": False, "error": code})


def test_requires_bot_token(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="SLACK_BOT_TOKEN"):
        SlackWorkspace(settings)


async def test_find_channel_pages_through_results(
    router: respx.MockRouter, slack: SlackWorkspace
) -> None:
    route = router.get("/conversations.list").mock(
        side_effect=[
            _ok(
                channels=[{"id": "C1", "name": "general"}], response_metadata={"next_cursor": "n2"}
            ),
            _ok(
                channels=[{"id": "C2", "name": "cs-escalations"}],
                response_metadata={"next_cursor": ""},
            ),
        ]
    )
    found = await slack.find_channel("cs-escalations")
    assert found is not None
    assert found.channel_id == "C2"
    assert _requests(route)[1].url.params["cursor"] == "n2"


async def test_find_channel_returns_none_when_absent(
    router: respx.MockRouter, slack: SlackWorkspace
) -> None:
    router.get("/conversations.list").mock(return_value=_ok(channels=[]))
    assert await slack.find_channel("nope") is None


async def test_post_message_returns_ts(router: respx.MockRouter, slack: SlackWorkspace) -> None:
    route = router.post("/chat.postMessage").mock(return_value=_ok(ts="1730.0001"))
    assert await slack.post_message("C2", "hello") == "1730.0001"
    sent = json.loads(route.calls.last.request.content)
    assert sent == {"channel": "C2", "text": "hello", "unfurl_links": False, "unfurl_media": False}


async def test_not_in_channel_is_permanent_with_a_hint(
    router: respx.MockRouter, slack: SlackWorkspace
) -> None:
    router.post("/chat.postMessage").mock(return_value=_err("not_in_channel"))
    with pytest.raises(PermanentError, match="invite the bot"):
        await slack.post_message("C2", "hello")


@pytest.mark.parametrize("code", ["invalid_auth", "token_revoked", "missing_scope"])
async def test_auth_errors(router: respx.MockRouter, slack: SlackWorkspace, code: str) -> None:
    router.post("/conversations.setTopic").mock(return_value=_err(code))
    with pytest.raises(AuthorizationError, match=code):
        await slack.set_topic("C2", "topic")


async def test_transient_error_is_retried_for_idempotent_calls(
    router: respx.MockRouter, slack: SlackWorkspace
) -> None:
    route = router.post("/conversations.setTopic").mock(side_effect=[_err("internal_error"), _ok()])
    await slack.set_topic("C2", "topic")
    assert route.call_count == 2


async def test_transient_error_on_post_is_outcome_unknown(
    router: respx.MockRouter, slack: SlackWorkspace
) -> None:
    route = router.post("/chat.postMessage").mock(return_value=_err("internal_error"))
    with pytest.raises(OutcomeUnknownError):
        await slack.post_message("C2", "hello")
    assert route.call_count == 1  # a retry could post the message twice


async def test_rate_limit_is_retried(router: respx.MockRouter, slack: SlackWorkspace) -> None:
    route = router.post("/chat.postMessage").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "1"}), _ok(ts="1")]
    )
    assert await slack.post_message("C2", "hello") == "1"
    assert route.call_count == 2


async def test_create_channel_name_taken(router: respx.MockRouter, slack: SlackWorkspace) -> None:
    router.post("/conversations.create").mock(return_value=_err("name_taken"))
    with pytest.raises(PermanentError, match="name_taken"):
        await slack.create_channel("onb-acme")


async def test_invite_members_tolerates_existing_membership(
    router: respx.MockRouter, slack: SlackWorkspace
) -> None:
    router.get("/users.lookupByEmail").mock(
        side_effect=[_ok(user={"id": "U1"}), _ok(user={"id": "U2"})]
    )
    invite = router.post("/conversations.invite").mock(
        side_effect=[_err("already_in_channel"), _ok()]
    )
    await slack.invite_members("C2", ["a@x.com", "b@x.com"])
    assert [json.loads(r.content)["users"] for r in _requests(invite)] == ["U1", "U2"]


async def test_invite_unknown_user_fails(router: respx.MockRouter, slack: SlackWorkspace) -> None:
    router.get("/users.lookupByEmail").mock(return_value=_err("users_not_found"))
    with pytest.raises(PermanentError, match="users_not_found"):
        await slack.invite_members("C2", ["ghost@x.com"])


async def test_slack_connect_is_mocked_when_disabled(
    router: respx.MockRouter, slack: SlackWorkspace
) -> None:
    route = router.post("/conversations.inviteShared")
    await slack.invite_external("C2", "customer@acme.com")
    assert route.call_count == 0


async def test_slack_connect_is_called_when_enabled(
    router: respx.MockRouter, settings: Settings
) -> None:
    route = router.post("/conversations.inviteShared").mock(return_value=_ok())
    workspace = _workspace(settings, connect=True)
    try:
        await workspace.invite_external("C2", "customer@acme.com")
    finally:
        await workspace.aclose()
    assert json.loads(route.calls.last.request.content) == {
        "channel": "C2",
        "emails": "customer@acme.com",
    }


def test_retryable_error_type_is_plain_for_reads() -> None:
    from app.integrations.slack import _raise_for_slack_error  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(RetryableError) as info:
        _raise_for_slack_error(
            "conversations.list", {"ok": False, "error": "service_unavailable"}, idempotent=True
        )
    assert type(info.value) is RetryableError
