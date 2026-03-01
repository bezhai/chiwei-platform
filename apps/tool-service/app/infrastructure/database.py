import logging

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.config.config import settings

logger = logging.getLogger(__name__)

_engine = None
_session_factory = None


def init_database() -> None:
    global _engine, _session_factory
    if not settings.database_url:
        logger.warning("DATABASE_URL not configured, database features disabled")
        return
    _engine = create_async_engine(settings.database_url, pool_size=5, max_overflow=5)
    _session_factory = sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
    logger.info("Database engine initialized")


async def close_database() -> None:
    global _engine
    if _engine:
        await _engine.dispose()
        _engine = None
    logger.info("Database engine closed")


def get_session_factory():
    return _session_factory
