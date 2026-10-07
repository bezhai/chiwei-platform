"""Async database session management.

Provides:
  - Module-level ``engine`` and ``async_session`` factory
  - ``get_session()`` async context manager with auto commit/rollback
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


def make_engine(url: URL) -> AsyncEngine:
    """The engine every database call in this process goes through, connected to ``url``."""
    return create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        future=True,
        pool_size=10,
        pool_recycle=3600,
        connect_args={"command_timeout": COMMAND_TIMEOUT_SECONDS},
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
