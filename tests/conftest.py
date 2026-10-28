from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from agents.tracing import set_trace_processors
from pydantic import SecretStr

from app.config import Settings
from app.db import Database
from app.integrations.fakes import (
    FakeChatWorkspace,
    FakeMailbox,
    FakeProjectStore,
    FakeVoiceCaller,
)
from app.models import Deal

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _no_trace_export() -> Iterator[None]:
    """Tests must never export traces to OpenAI."""
    set_trace_processors([])
    yield
    set_trace_processors([])


@pytest.fixture
def ae_directory_file(tmp_path: Path) -> Path:
    path = tmp_path / "ae_directory.yaml"
    path.write_text(
        "account_executives:\n"
        "  - name: Ravi Kumar\n"
        "    email: ravi.ae@example.com\n"
        '    phone: "+919876543210"\n',
        encoding="utf-8",
    )
    return path


@pytest.fixture
def settings(tmp_path: Path, ae_directory_file: Path) -> Settings:
    return Settings(
        _env_file=None,  # pyright: ignore[reportCallIssue]
        env="test",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}",
        admin_token=SecretStr("test-admin-token-123"),
        templates_path=REPO_ROOT / "config" / "templates.yaml",
        ae_directory_path=ae_directory_file,
    )


@pytest.fixture
async def db(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings.database_url)
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
async def deal(db: Database) -> Deal:
    async with db.session() as session:
        new = Deal(
            gmail_message_id="msg-1",
            gmail_thread_id="thread-1",
            sender_email="ravi.ae@example.com",
        )
        session.add(new)
    return new


@pytest.fixture
def mailbox() -> FakeMailbox:
    return FakeMailbox()


@pytest.fixture
def voice() -> FakeVoiceCaller:
    return FakeVoiceCaller()


@pytest.fixture
def projects() -> FakeProjectStore:
    return FakeProjectStore()


@pytest.fixture
def chat() -> FakeChatWorkspace:
    return FakeChatWorkspace()
