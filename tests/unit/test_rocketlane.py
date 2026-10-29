from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import date

import httpx
import pytest
import respx
from pydantic import SecretStr

from app.config import Settings
from app.domain import PlanTier
from app.errors import ConfigError, OutcomeUnknownError, PermanentError
from app.integrations.http import HttpService
from app.integrations.retry import RetryPolicy
from app.integrations.rocketlane import RocketlaneProjects, build_create_request
from app.ports import NewProject

BASE = "https://rocketlane.test/api/1.0"
PROJECT = NewProject(
    deal_id="d_1",
    project_name="Acme Corp - Enterprise onboarding",
    customer_company="Acme Corp",
    plan_tier=PlanTier.ENTERPRISE,
    template_id=111,
    start_date=date(2026, 10, 28),
    owner_email="priya@x.com",
    csm_email="anita@x.com",
)


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
async def rocketlane(settings: Settings) -> AsyncIterator[RocketlaneProjects]:
    async def no_sleep(_seconds: float) -> None:
        return None

    http = HttpService(
        httpx.AsyncClient(base_url=BASE),
        name="rocketlane",
        policy=RetryPolicy(attempts=3, base_seconds=0, max_seconds=0),
        max_concurrency=2,
        sleep=no_sleep,
    )
    store = RocketlaneProjects(
        settings.model_copy(update={"rocketlane_api_key": SecretStr("rl-key")}), http=http
    )
    yield store
    await store.aclose()


def _requests(route: respx.Route) -> list[httpx.Request]:
    return [call.request for call in route.calls]  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


async def test_real_client_sends_the_api_key(router: respx.MockRouter, settings: Settings) -> None:
    route = router.get("/projects").respond(200, json={"data": [], "pagination": {}})
    store = RocketlaneProjects(
        settings.model_copy(
            update={"rocketlane_api_key": SecretStr("rl-key"), "rocketlane_base_url": BASE}
        )
    )
    try:
        await store.list_projects()
    finally:
        await store.aclose()
    assert _requests(route)[0].headers["api-key"] == "rl-key"


def test_requires_api_key(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="ROCKETLANE_API_KEY"):
        RocketlaneProjects(settings)


# --- create request ----------------------------------------------------------------


def test_minimal_create_request() -> None:
    assert build_create_request(PROJECT) == {
        "projectName": "Acme Corp - Enterprise onboarding",
        "owner": {"emailId": "priya@x.com"},
        "customer": {"companyName": "Acme Corp"},
        "autoCreateCompany": True,
        "startDate": "2026-10-28",
        "sources": [{"templateId": 111, "startDate": "2026-10-28"}],
        "teamMembers": {"members": [{"emailId": "anita@x.com"}]},
        "assignProjectOwner": False,
        "externalReferenceId": "d_1",
    }


def test_full_create_request() -> None:
    full = PROJECT.model_copy(
        update={
            "csm_placeholder_id": 77,
            "customer_contact_email": "jane@acme.com",
            "assign_unassigned_tasks_to_owner": True,
            "custom_fields": ((9, "Enterprise"),),
        }
    )
    body = build_create_request(full)
    assert body["placeholders"] == [{"placeholderId": 77, "user": {"emailId": "anita@x.com"}}]
    assert body["teamMembers"]["customers"] == [{"emailId": "jane@acme.com"}]
    assert body["fields"] == [{"fieldId": 9, "fieldValue": "Enterprise"}]
    assert body["assignProjectOwner"] is True


# --- list --------------------------------------------------------------------------


async def test_list_pages_and_reads_markers(
    router: respx.MockRouter, rocketlane: RocketlaneProjects
) -> None:
    route = router.get("/projects").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "data": [
                        {"projectId": 1, "projectName": "A", "customer": {"companyName": "Acme"}},
                    ],
                    "pagination": {"hasMore": True, "nextPageToken": "p2"},
                },
            ),
            httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "projectId": 2,
                            "projectName": "B",
                            "customer": {"companyName": "Globex"},
                            "externalReferenceId": "d_9",
                            "archived": True,
                        }
                    ],
                    "pagination": {"hasMore": False},
                },
            ),
        ]
    )

    projects = await rocketlane.list_projects()

    assert [(p.project_id, p.customer_company, p.deal_marker, p.is_active) for p in projects] == [
        ("1", "Acme", None, True),
        ("2", "Globex", "d_9", False),
    ]
    first, second = _requests(route)
    assert first.url.params["includeFields"] == "externalReferenceId"
    assert second.url.params["pageToken"] == "p2"


async def test_list_never_returns_a_partial_result(
    router: respx.MockRouter, rocketlane: RocketlaneProjects, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.integrations.rocketlane.MAX_PAGES", 2)
    router.get("/projects").mock(
        return_value=httpx.Response(
            200, json={"data": [], "pagination": {"hasMore": True, "nextPageToken": "more"}}
        )
    )
    with pytest.raises(PermanentError, match="more than"):
        await rocketlane.list_projects()


async def test_list_retries_when_rocketlane_is_down(
    router: respx.MockRouter, rocketlane: RocketlaneProjects
) -> None:
    route = router.get("/projects").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"data": [], "pagination": {}})]
    )
    assert await rocketlane.list_projects() == []
    assert route.call_count == 2


# --- create ------------------------------------------------------------------------


async def test_create_returns_the_project(
    router: respx.MockRouter, rocketlane: RocketlaneProjects
) -> None:
    route = router.post("/projects").respond(
        201, json={"projectId": 5000000250001, "projectName": PROJECT.project_name}
    )
    ref = await rocketlane.create_project(PROJECT)
    assert (ref.project_id, ref.deal_marker, ref.customer_company) == (
        "5000000250001",
        "d_1",
        "Acme Corp",
    )
    assert json.loads(_requests(route)[0].content)["externalReferenceId"] == "d_1"


async def test_create_validation_error_is_permanent_with_rocketlane_message(
    router: respx.MockRouter, rocketlane: RocketlaneProjects
) -> None:
    router.post("/projects").respond(
        400,
        json={
            "errors": [
                {"errorCode": "BAD_REQUEST", "errorMessage": "Owner not found", "field": "owner"}
            ]
        },
    )
    with pytest.raises(PermanentError, match="Owner not found"):
        await rocketlane.create_project(PROJECT)


async def test_create_server_error_is_never_blindly_retried(
    router: respx.MockRouter, rocketlane: RocketlaneProjects
) -> None:
    route = router.post("/projects").respond(500)
    with pytest.raises(OutcomeUnknownError):
        await rocketlane.create_project(PROJECT)
    assert route.call_count == 1


async def test_create_timeout_is_outcome_unknown(
    router: respx.MockRouter, rocketlane: RocketlaneProjects
) -> None:
    router.post("/projects").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OutcomeUnknownError):
        await rocketlane.create_project(PROJECT)


async def test_create_rate_limit_is_retried(
    router: respx.MockRouter, rocketlane: RocketlaneProjects
) -> None:
    route = router.post("/projects").mock(
        side_effect=[httpx.Response(429), httpx.Response(201, json={"projectId": 7})]
    )
    assert (await rocketlane.create_project(PROJECT)).project_id == "7"
    assert route.call_count == 2


async def test_create_without_id_is_permanent(
    router: respx.MockRouter, rocketlane: RocketlaneProjects
) -> None:
    router.post("/projects").respond(201, json={"projectName": "x"})
    with pytest.raises(PermanentError, match="no projectId"):
        await rocketlane.create_project(PROJECT)


async def test_real_client_uses_the_longer_rocketlane_timeout(settings: Settings) -> None:
    store = RocketlaneProjects(
        settings.model_copy(
            update={"rocketlane_api_key": SecretStr("k"), "rocketlane_read_timeout_seconds": 90.0}
        )
    )
    timeout = store._http._client.timeout  # pyright: ignore[reportPrivateUsage]
    await store.aclose()
    assert (timeout.read, timeout.connect) == (90.0, settings.http_connect_timeout_seconds)


def test_due_date_is_left_to_the_template() -> None:
    assert "dueDate" not in build_create_request(PROJECT)
