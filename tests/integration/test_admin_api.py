"""Admin API: auth on every route, deal views, audit timeline and traces."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.integrations.fakes import FakeJobScheduler, FakeTierVerifier, FakeVoiceCaller
from app.main import create_app
from app.models import AgentSpan, AgentTrace, Deal
from app.orchestrator.escalation import escalate
from app.orchestrator.runtime import Runtime

ADMIN = {"x-admin-token": "test-admin-token-123"}
TEST_DEAL = {
    "customer_company": "Acme Corp",
    "ae_name": "Ravi Kumar",
    "ae_email": "ravi.ae@novacrm-demo.com",
    "ae_phone": "+919876543210",
}
READ_ROUTES = ["/deals", "/deals/{id}", "/deals/{id}/audit", "/deals/{id}/traces"]


@pytest.fixture
def jobs() -> FakeJobScheduler:
    return FakeJobScheduler()


@pytest.fixture
def app(settings: Settings, jobs: FakeJobScheduler) -> FastAPI:
    return create_app(
        settings, voice=FakeVoiceCaller(), tier_verifier=FakeTierVerifier(), jobs=jobs
    )


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _create_deal(client: TestClient, **overrides: str) -> str:
    response = client.post("/admin/test-deals", json={**TEST_DEAL, **overrides}, headers=ADMIN)
    assert response.status_code == 201
    return response.json()["deal_id"]


@pytest.mark.parametrize("route", READ_ROUTES)
@pytest.mark.parametrize("headers", [{}, {"x-admin-token": "nope-nope-nope-nope"}])
def test_read_routes_require_admin(client: TestClient, route: str, headers: dict[str, str]) -> None:
    deal_id = _create_deal(client)
    assert client.get(route.format(id=deal_id), headers=headers).status_code == 401


@pytest.mark.parametrize("route", READ_ROUTES[1:])
def test_unknown_deal_is_404(client: TestClient, route: str) -> None:
    assert client.get(route.format(id="d_missing"), headers=ADMIN).status_code == 404


def test_list_filters_by_status(client: TestClient) -> None:
    first = _create_deal(client)
    second = _create_deal(client, customer_company="Globex")

    everything = client.get("/deals", headers=ADMIN).json()
    waiting = client.get("/deals", params={"status": "awaiting_tier"}, headers=ADMIN).json()
    escalated = client.get("/deals", params={"status": "escalated"}, headers=ADMIN).json()

    assert {d["id"] for d in everything} == {first, second}
    assert len(waiting) == 2
    assert escalated == []


def test_list_rejects_unknown_status(client: TestClient) -> None:
    assert client.get("/deals", params={"status": "bogus"}, headers=ADMIN).status_code == 422


def test_deal_detail(client: TestClient) -> None:
    deal_id = _create_deal(client)
    body = client.get(f"/deals/{deal_id}", headers=ADMIN).json()
    assert body["id"] == deal_id
    assert body["status"] == "awaiting_tier"
    assert body["customer_company"] == "Acme Corp"
    assert body["call_attempts"] == []
    assert body["version"] == 2


def test_audit_timeline_is_in_order(client: TestClient) -> None:
    deal_id = _create_deal(client)
    rows = client.get(f"/deals/{deal_id}/audit", headers=ADMIN).json()
    assert [r["action"] for r in rows] == ["transition"]
    assert rows[0]["input_json"]["to"] == "awaiting_tier"
    assert rows[0]["actor"] == "admin"


async def test_traces_are_grouped_by_deal(client: TestClient, app: FastAPI) -> None:
    deal_id = _create_deal(client)
    app_db: Database = app.state.db
    async with app_db.session() as session:
        session.add(AgentTrace(trace_id="tr_1", name="voice_call_decision", group_id=deal_id))
        session.add(AgentTrace(trace_id="tr_other", name="x", group_id="d_other"))
        session.add(AgentSpan(span_id="sp_1", trace_id="tr_1", span_type="agent"))
    traces = client.get(f"/deals/{deal_id}/traces", headers=ADMIN).json()
    assert [t["trace_id"] for t in traces] == ["tr_1"]
    assert [s["span_id"] for s in traces[0]["spans"]] == ["sp_1"]


# --- resolve -----------------------------------------------------------------------


async def _escalate(app: FastAPI, deal_id: str) -> None:
    runtime: Runtime = app.state.runtime
    async with runtime.db.session() as session:
        deal = await session.get(Deal, deal_id)
        assert deal is not None
        await escalate(session, deal, actor="voice_agent", reason="tier not confirmed")


def _resolve(client: TestClient, deal_id: str, **body: object):
    payload = {"actor": "Priya", "reason": "Confirmed with Ravi on Slack", **body}
    return client.post(f"/deals/{deal_id}/resolve", json=payload, headers=ADMIN)


async def test_confirm_tier_records_a_human_override(client: TestClient, app: FastAPI) -> None:
    deal_id = _create_deal(client)
    await _escalate(app, deal_id)

    response = _resolve(client, deal_id, action="confirm_tier", plan_tier="growth")

    assert response.status_code == 200
    body = response.json()
    assert body["deal"]["status"] == "tier_confirmed"
    assert body["deal"]["plan_tier"] == "growth"
    assert body["deal"]["tier_source"] == "human_override"
    audit = client.get(f"/deals/{deal_id}/audit", headers=ADMIN).json()
    resolve_row = next(r for r in audit if r["action"] == "resolve")
    assert resolve_row["actor"] == "human:Priya"
    assert resolve_row["rationale"] == "Confirmed with Ravi on Slack"


async def test_retry_call_queues_another_attempt(
    client: TestClient, app: FastAPI, jobs: FakeJobScheduler
) -> None:
    deal_id = _create_deal(client)
    jobs.jobs.clear()
    await _escalate(app, deal_id)

    response = _resolve(client, deal_id, action="retry_call")

    assert response.json()["deal"]["status"] == "awaiting_tier"
    assert response.json()["next"] == "call_queued"
    assert f"call:{deal_id}:1" in jobs.jobs


@pytest.mark.parametrize(
    ("action", "status"), [("dismiss", "ignored"), ("reprocess_intake", "received")]
)
async def test_other_resolutions(
    client: TestClient, app: FastAPI, action: str, status: str
) -> None:
    deal_id = _create_deal(client)
    await _escalate(app, deal_id)
    assert _resolve(client, deal_id, action=action).json()["deal"]["status"] == status


@pytest.mark.parametrize(
    "body",
    [
        {"action": "confirm_tier"},  # tier missing
        {"action": "retry_call", "plan_tier": "growth"},  # tier not allowed
        {"action": "confirm_tier", "plan_tier": "platinum"},
        {"action": "explode"},
        {"action": "dismiss", "reason": "no"},  # reason too short
        {"action": "dismiss", "actor": ""},
    ],
)
async def test_resolution_is_validated(
    client: TestClient, app: FastAPI, body: dict[str, object]
) -> None:
    deal_id = _create_deal(client)
    await _escalate(app, deal_id)
    assert _resolve(client, deal_id, **body).status_code == 422


def test_only_escalated_deals_can_be_resolved(client: TestClient) -> None:
    deal_id = _create_deal(client)  # awaiting_tier
    response = _resolve(client, deal_id, action="dismiss")
    assert response.status_code == 409
    assert "only escalated deals" in response.json()["detail"]


def test_resolving_unknown_deal_is_404(client: TestClient) -> None:
    assert _resolve(client, "d_missing", action="dismiss").status_code == 404


def test_resolve_requires_admin(client: TestClient) -> None:
    response = client.post("/deals/d_x/resolve", json={"action": "dismiss"})
    assert response.status_code == 401
