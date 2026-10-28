from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.errors import ConfigError
from app.integrations.fakes import FakeMailbox
from app.main import create_app
from app.orchestrator.poller import InboxPoller


def test_health_ok(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": [{"name": "database", "ok": True, "detail": None}],
    }


def _gmail_settings(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(update={"gmail_enabled": True, **overrides})


def test_inbox_health_is_reported_when_polling(settings: Settings) -> None:
    with TestClient(create_app(_gmail_settings(settings), mailbox=FakeMailbox())) as client:
        response = client.get("/health")
    assert response.status_code == 200
    checks = {c["name"]: c for c in response.json()["checks"]}
    assert checks["gmail"]["ok"] is True


def test_inbox_auth_failure_degrades_health(settings: Settings) -> None:
    app = create_app(_gmail_settings(settings), mailbox=FakeMailbox())
    with TestClient(app) as client:
        poller: InboxPoller = app.state.poller
        poller.status.auth_failed = True
        poller.status.last_error = "Gmail token refresh rejected: invalid_grant"
        response = client.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    gmail = next(c for c in body["checks"] if c["name"] == "gmail")
    assert gmail == {"name": "gmail", "ok": False, "detail": poller.status.last_error}


def test_polling_without_allowlist_refuses_to_start(settings: Settings) -> None:
    unsafe = _gmail_settings(settings, allowed_sender_domains=[], allowed_sender_emails=[])
    with (
        pytest.raises(ConfigError, match="ALLOWED_SENDER"),
        TestClient(create_app(unsafe, mailbox=FakeMailbox())),
    ):
        pass
