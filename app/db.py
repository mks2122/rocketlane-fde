"""Database engines and unit-of-work sessions.

Two engines share one database:
- async engine for request/workflow code;
- sync engine for callers that cannot await (Agents SDK trace processor,
  APScheduler job store). SQLite runs in WAL mode so both can coexist.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlmodel import Session, SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

import app.models  # noqa: F401  # pyright: ignore[reportUnusedImport]  # registers tables
from app.errors import ConfigError

_SYNC_DRIVERS = {
    "sqlite+aiosqlite": "sqlite",
    "postgresql+asyncpg": "postgresql+psycopg",
}


def sync_url_for(async_url: str) -> str:
    url = make_url(async_url)
    driver = _SYNC_DRIVERS.get(url.drivername)
    if driver is None:
        raise ConfigError(f"unsupported database driver: {url.drivername}")
    return url.set(drivername=driver).render_as_string(hide_password=False)


def _enable_sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.is_sqlite = make_url(url).get_backend_name() == "sqlite"
        if self.is_sqlite:
            self._ensure_sqlite_dir(url)

        self.async_engine: AsyncEngine = create_async_engine(url)
        self.sync_engine: Engine = create_engine(sync_url_for(url))
        if self.is_sqlite:
            event.listen(self.async_engine.sync_engine, "connect", _enable_sqlite_pragmas)
            event.listen(self.sync_engine, "connect", _enable_sqlite_pragmas)

        self._session_factory = async_sessionmaker(
            self.async_engine, class_=AsyncSession, expire_on_commit=False
        )

    @staticmethod
    def _ensure_sqlite_dir(url: str) -> None:
        database = make_url(url).database
        if database and database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)

    async def create_all(self) -> None:
        async with self.async_engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def session(self) -> AsyncGenerator[AsyncSession, None]:
        """Unit of work: commits on success, rolls back on any exception."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    @contextmanager
    def sync_session(self) -> Generator[Session, None, None]:
        with Session(self.sync_engine) as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    async def ping(self) -> bool:
        async with self.async_engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    async def dispose(self) -> None:
        await self.async_engine.dispose()
        self.sync_engine.dispose()
