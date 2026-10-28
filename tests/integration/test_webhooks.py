"""HTTP surface of the voice step: webhook auth and shape, and the admin test-deal endpoint."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.config import Settings
from app.integrations.fakes import FakeJobScheduler, FakeTierVerifier, FakeVoiceCaller
from app.main import create_app

SECRET = "hook-secret-123"
ADMIN = "test-admin-token-123"


@pytest.fixture
def jobs() -> FakeJobScheduler:
    return FakeJobScheduler()


@pytest.fixture
def client(
    settings: Settings, jobs: FakeJobScheduler, voice: FakeVoiceCaller
) -> Iterator[TestClient]:
    configured = settings.model_copy(update={"vapi_webhook_secret": SecretStr(SECRET)})
    app = create_app(configured, voice=voice, tier_verifier=FakeTierVerifier(), jobs=jobs)
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize("secret", [None, "", "wrong"])
def test_webhook_rejects_bad_secret_before_parsing(client: TestClient, secret: str | None) -> None:
    headers = {"x-vapi-secret": secret} if secret is not None else {}
    response = client.post("/webhooks/vapi", content=b"{not json", headers=headers)
    assert response.status_code == 401


def test_webhook_fails_closed_without_configured_secret(settings: Settings) -> None:
    app = create_app(settings, voice=FakeVoiceCaller(), jobs=FakeJobScheduler())
    with TestClient(app) as unconfigured:
        response = unconfigured.post(
            "/webhooks/vapi",
            json={"message": {"type": "status-update"}},
            headers={"x-vapi-secret": ""},
        )
    assert response.status_code == 401


def test_webhook_rejects_malformed_payload(client: TestClient) -> None:
    response = client.post(
        "/webhooks/vapi", content=b'{"no_message": 1}', headers={"x-vapi-secret": SECRET}
    )
    assert response.status_code == 400


def test_tool_call_gets_a_result_for_every_call(client: TestClient) -> None:
    body: dict[str, object] = {
        "message": {
            "type": "tool-calls",
            "call": {"id": "unknown-call"},
            "toolCallList": [
                {"id": "tc_1", "function": {"name": "record_plan_tier", "arguments": "{}"}},
                {"id": "tc_2", "function": {"name": "something_else", "arguments": {}}},
            ],
        }
    }
    response = client.post("/webhooks/vapi", json=body, headers={"x-vapi-secret": SECRET})
    assert response.status_code == 200
    results = response.json()["results"]
    assert [r["toolCallId"] for r in results] == ["tc_1", "tc_2"]
    assert all(r["result"].startswith("error:") for r in results)


def test_informational_messages_are_acknowledged(client: TestClient) -> None:
    for message_type in ("status-update", "speech-update", "conversation-update"):
        response = client.post(
            "/webhooks/vapi",
            json={"message": {"type": message_type, "status": "ringing"}},
            headers={"x-vapi-secret": SECRET},
        )
        assert response.status_code == 200
        assert response.json() == {}


# --- admin -------------------------------------------------------------------------

TEST_DEAL = {
    "customer_company": "Acme Corp",
    "ae_name": "Ravi Kumar",
    "ae_email": "ravi.ae@novacrm-demo.com",
    "ae_phone": "+919876543210",
}


@pytest.mark.parametrize("token", [None, "wrong-token-wrong-token"])
def test_admin_requires_token(client: TestClient, token: str | None) -> None:
    headers = {"x-admin-token": token} if token else {}
    assert client.post("/admin/test-deals", json=TEST_DEAL, headers=headers).status_code == 401


def test_test_deal_is_queued_for_a_call(client: TestClient, jobs: FakeJobScheduler) -> None:
    response = client.post("/admin/test-deals", json=TEST_DEAL, headers={"x-admin-token": ADMIN})
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "awaiting_tier"
    assert f"call:{body['deal_id']}:1" in jobs.jobs


def test_test_deal_validates_phone(client: TestClient) -> None:
    bad = {**TEST_DEAL, "ae_phone": "98765 43210"}
    response = client.post("/admin/test-deals", json=bad, headers={"x-admin-token": ADMIN})
    assert response.status_code == 422


def test_test_deals_are_disabled_in_prod(settings: Settings) -> None:
    prod = settings.model_copy(update={"env": "prod"})
    app = create_app(prod, voice=FakeVoiceCaller(), jobs=FakeJobScheduler())
    with TestClient(app) as prod_client:
        response = prod_client.post(
            "/admin/test-deals", json=TEST_DEAL, headers={"x-admin-token": ADMIN}
        )
    assert response.status_code == 404
