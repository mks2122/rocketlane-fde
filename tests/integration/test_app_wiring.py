"""The assembled app: adapters reach the runtime, and inbox auth failures alert Slack."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.integrations.fakes import FakeChatWorkspace, FakeJobScheduler, FakeMailbox
from app.integrations.gmail import GmailAuthError
from app.main import create_app
from app.orchestrator.poller import InboxPoller
from app.orchestrator.runtime import Runtime
from app.ports import ChannelRef


def test_runtime_gets_the_notification_channels(settings: Settings) -> None:
    mailbox, chat = FakeMailbox(), FakeChatWorkspace()
    enabled = settings.model_copy(update={"gmail_enabled": True})
    app = create_app(enabled, mailbox=mailbox, chat=chat, jobs=FakeJobScheduler())
    with TestClient(app):
        runtime: Runtime = app.state.runtime
        assert runtime.mailbox is mailbox
        assert runtime.chat is chat


def test_gmail_disabled_means_no_mailbox(settings: Settings) -> None:
    app = create_app(settings, chat=FakeChatWorkspace(), jobs=FakeJobScheduler())
    with TestClient(app):
        runtime: Runtime = app.state.runtime
        assert runtime.mailbox is None
        assert app.state.poller is None


def test_inbox_auth_failure_alerts_slack(settings: Settings) -> None:
    mailbox, chat = FakeMailbox(), FakeChatWorkspace()
    chat.channels["cs-escalations"] = ChannelRef(channel_id="C_ESC", name="cs-escalations")
    enabled = settings.model_copy(update={"gmail_enabled": True})
    app = create_app(enabled, mailbox=mailbox, chat=chat, jobs=FakeJobScheduler())
    with TestClient(app) as client:
        poller: InboxPoller = app.state.poller
        mailbox.fail("list_new_message_ids", GmailAuthError("invalid_grant"))
        client.portal.call(poller.poll_once)  # pyright: ignore[reportOptionalMemberAccess]

    [alert] = [text for texts in chat.messages.values() for text in texts]
    assert "CS inbox" in alert
    assert "invalid_grant" in alert
    assert "gmail_auth.py" in alert
