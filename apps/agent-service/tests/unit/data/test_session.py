"""Tests for app.data.session — engine, session factory, get_session()."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


class TestDatabaseURL:
    """DATABASE_URL should be constructed from infra settings."""

    def test_uses_asyncpg_driver(self):
        from app.data.session import DATABASE_URL

        assert DATABASE_URL.drivername == "postgresql+asyncpg"

    def test_ssl_disable_in_query(self):
        from app.data.session import DATABASE_URL

        assert DATABASE_URL.query.get("ssl") == "disable"


class TestEngineConfig:
    """Engine should have correct pool settings."""

    def test_engine_pool_size(self):
        from app.data.session import engine

        assert engine.pool.size() == 10

    def test_engine_echo_off(self):
        from app.data.session import engine

        assert engine.echo is False


class TestAsyncSessionFactory:
    """async_session factory should produce AsyncSession instances."""

    def test_factory_expire_on_commit_false(self):
        from app.data.session import async_session

        assert async_session.kw.get("expire_on_commit") is False


class TestGetSession:
    """get_session() context manager commit/rollback behavior."""

    async def test_commits_on_success(self):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_factory = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        with patch("app.data.session.async_session", mock_factory):
            from app.data.session import get_session

            async with get_session() as session:
                assert session is mock_session

            mock_session.commit.assert_awaited_once()
            mock_session.rollback.assert_not_awaited()

    async def test_rolls_back_on_error(self):
        mock_session = AsyncMock()
        mock_session.commit = AsyncMock()
        mock_session.rollback = AsyncMock()

        mock_factory = MagicMock()
        ctx = AsyncMock()
        ctx.__aenter__ = AsyncMock(return_value=mock_session)
        ctx.__aexit__ = AsyncMock(return_value=False)
        mock_factory.return_value = ctx

        with patch("app.data.session.async_session", mock_factory):
            from app.data.session import get_session

            try:
                async with get_session() as session:
                    assert session is mock_session
                    raise ValueError("boom")
            except ValueError:
                pass

            mock_session.rollback.assert_awaited_once()
            mock_session.commit.assert_not_awaited()
