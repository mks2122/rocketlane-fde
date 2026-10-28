from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime

import httpx
import pytest
import respx

from app.errors import OutcomeUnknownError, PermanentError, RetryableError
from app.integrations.http import HttpService, parse_retry_after, raise_for_status
from app.integrations.retry import RetryPolicy

BASE = "https://api.example.test/v1"


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def sleeps() -> list[float]:
    return []


def _service(sleeps: list[float], *, attempts: int = 3, concurrency: int = 4) -> HttpService:
    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return HttpService(
        httpx.AsyncClient(base_url=BASE, timeout=5),
        name="svc",
        policy=RetryPolicy(attempts=attempts, base_seconds=1.0, max_seconds=8.0),
        max_concurrency=concurrency,
        sleep=record_sleep,
    )


@pytest.fixture
async def svc(sleeps: list[float]) -> AsyncIterator[HttpService]:
    service = _service(sleeps)
    yield service
    await service.aclose()


# --- idempotent requests -------------------------------------------------------


async def test_success_is_returned(router: respx.MockRouter, svc: HttpService) -> None:
    router.get("/things").respond(200, json={"ok": True})
    response = await svc.request("GET", "/things")
    assert response.json() == {"ok": True}


async def test_5xx_is_retried_with_backoff(
    router: respx.MockRouter, svc: HttpService, sleeps: list[float]
) -> None:
    route = router.get("/things").mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    await svc.request("GET", "/things")
    assert route.call_count == 2
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 8.0


async def test_retries_are_bounded(
    router: respx.MockRouter, svc: HttpService, sleeps: list[float]
) -> None:
    route = router.get("/things").respond(502)
    with pytest.raises(RetryableError, match="HTTP 502"):
        await svc.request("GET", "/things")
    assert route.call_count == 3
    assert len(sleeps) == 2


async def test_4xx_is_permanent_and_not_retried(router: respx.MockRouter, svc: HttpService) -> None:
    route = router.get("/things").respond(404, text="no such project")
    with pytest.raises(PermanentError, match=r"HTTP 404.*no such project"):
        await svc.request("GET", "/things")
    assert route.call_count == 1


async def test_retry_after_is_honoured_and_capped(
    router: respx.MockRouter, svc: HttpService, sleeps: list[float]
) -> None:
    router.get("/things").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(429, headers={"Retry-After": "3600"}),
            httpx.Response(200),
        ]
    )
    await svc.request("GET", "/things")
    assert sleeps == [2.0, 8.0]


async def test_read_timeout_on_idempotent_request_is_retried(
    router: respx.MockRouter, svc: HttpService
) -> None:
    route = router.get("/things").mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)])
    await svc.request("GET", "/things")
    assert route.call_count == 2


# --- non-idempotent requests (creates) -----------------------------------------


async def test_create_5xx_is_outcome_unknown_and_not_retried(
    router: respx.MockRouter, svc: HttpService
) -> None:
    route = router.post("/things").respond(500)
    with pytest.raises(OutcomeUnknownError):
        await svc.request("POST", "/things", idempotent=False)
    assert route.call_count == 1


async def test_create_read_timeout_is_outcome_unknown_and_not_retried(
    router: respx.MockRouter, svc: HttpService
) -> None:
    route = router.post("/things").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(OutcomeUnknownError, match="after sending POST"):
        await svc.request("POST", "/things", idempotent=False)
    assert route.call_count == 1


async def test_create_connect_error_is_retried_because_nothing_was_sent(
    router: respx.MockRouter, svc: HttpService
) -> None:
    route = router.post("/things").mock(
        side_effect=[httpx.ConnectError("refused"), httpx.Response(201)]
    )
    response = await svc.request("POST", "/things", idempotent=False)
    assert response.status_code == 201
    assert route.call_count == 2


async def test_create_429_is_retried_because_it_was_refused(
    router: respx.MockRouter, svc: HttpService
) -> None:
    route = router.post("/things").mock(side_effect=[httpx.Response(429), httpx.Response(201)])
    await svc.request("POST", "/things", idempotent=False)
    assert route.call_count == 2


# --- concurrency ---------------------------------------------------------------


async def test_concurrency_is_capped(router: respx.MockRouter, sleeps: list[float]) -> None:
    in_flight = 0
    peak = 0

    async def slow(_request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200)

    router.get("/things").mock(side_effect=slow)
    service = _service(sleeps, concurrency=2)
    try:
        await asyncio.gather(*(service.request("GET", "/things") for _ in range(6)))
    finally:
        await service.aclose()
    assert peak == 2


# --- pure helpers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ("", None), ("7", 7.0), ("soon", None)],
)
def test_parse_retry_after_seconds(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_parse_retry_after_http_date() -> None:
    now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=UTC)
    assert parse_retry_after("Wed, 28 Oct 2026 12:00:30 GMT", now=now) == 30.0
    assert parse_retry_after("Wed, 28 Oct 2026 11:00:00 GMT", now=now) == 0.0


def test_raise_for_status_passes_success() -> None:
    request = httpx.Request("GET", f"{BASE}/x")
    raise_for_status(httpx.Response(204, request=request), service="svc")


def test_error_body_is_truncated() -> None:
    request = httpx.Request("GET", f"{BASE}/x")
    with pytest.raises(PermanentError) as info:
        raise_for_status(httpx.Response(400, text="x" * 5000, request=request), service="svc")
    assert len(str(info.value)) < 400
