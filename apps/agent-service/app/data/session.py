"""Async database session management.

Provides:
  - Module-level ``engine`` and ``async_session`` factory
  - ``get_session()`` async context manager with auto commit/rollback

Every database call in this process goes through the one engine built by :func:`make_engine`,
so the time limits below apply to all of them: sessions from ``get_session()``, the schema
work at startup, and the pool's own pings, resets and closes.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.data.dialect import DRIVERNAME
from app.infra.config import settings

DATABASE_URL = URL.create(
    "postgresql+asyncpg",
    username=settings.postgres_user,
    password=settings.postgres_password,
    host=settings.postgres_host,
    port=settings.postgres_port,
    database=settings.postgres_db,
    query={"ssl": "disable"},
)

# asyncpg's ``command_timeout``: the longest any single operation on a connection may take,
# every statement and also closing the connection. Closing waits for the server to drop its end;
# when that never reaches this process (seen on the coe network path, 2026-10-06), the close
# would otherwise wait forever, and the caller with it, outside any timeout of its own. Overflow
# connections are closed every time they are returned, so a burst of more concurrent sessions
# than ``pool_size`` hits this path.
COMMAND_TIMEOUT_SECONDS = 60

# The server's own ``statement_timeout``, set on every connection: a statement running this long is
# stopped by the server, which answers with an error on the same connection. Below
# ``COMMAND_TIMEOUT_SECONDS`` on purpose. When the client's limit fires instead, asyncpg has to ask
# the server to stop the statement over a second connection and then waits, with no limit of its
# own, for the server to close that connection; on a network path that loses the close, every later
# step on the connection waits with it (see :mod:`app.data.dialect`). With the server stopping
# slow statements first, the client's limit is left as the backstop for a server that does not
# answer at all and for closing connections. The 10 s between them is for the server's error to
# travel back.
STATEMENT_TIMEOUT_SECONDS = 50


def make_engine(url: URL) -> AsyncEngine:
    """The engine every database call in this process goes through, connected to ``url``
    (a ``postgresql+asyncpg`` URL; the engine uses the dialect in :mod:`app.data.dialect`)."""
    return create_async_engine(
        url.set(drivername=DRIVERNAME),
        echo=False,
        pool_pre_ping=True,
        future=True,
        pool_size=10,
        pool_recycle=3600,
        connect_args={
            "command_timeout": COMMAND_TIMEOUT_SECONDS,
            "server_settings": {"statement_timeout": f"{int(STATEMENT_TIMEOUT_SECONDS * 1000)}"},
        },
    )


engine = make_engine(DATABASE_URL)

async_session = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an ``AsyncSession`` that auto-commits on success, rolls back on error."""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
