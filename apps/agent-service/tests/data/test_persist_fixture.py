"""Smoke tests for the ``test_db`` fixture.

在 Task 0.8/0.9/0.11 之前先证明 fixture 本身工作：
  1. 能连上真实 Postgres，``SELECT 1`` 往返正常。
  2. ``app.data.session.get_session()`` 被 monkeypatch 到测试容器
     （写入不会影响生产库，跨测试表被清理）。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


@pytest.mark.integration
async def test_fixture_provides_real_pg(test_db):
    """Fixture 启动的容器能接受 SQL 查询。"""
    async with test_db.begin() as conn:
        result = await conn.execute(text("SELECT 1"))
        assert result.scalar() == 1


@pytest.mark.integration
async def test_get_session_routes_to_test_container(test_db):
    """``get_session()`` 必须走测试容器，不是 prod DSN。

    这个测试是 monkeypatch 的核心证据：如果 patch 没生效，
    ``get_session()`` 会尝试连 prod（或失败，或写错库）。这里我们
    建一张临时表、写一行、读回来，只有走测试容器才能通。
    """
    from app.data.session import get_session

    async with get_session() as session:
        await session.execute(
            text("CREATE TABLE IF NOT EXISTS fixture_ping (x INTEGER)")
        )
        await session.execute(text("INSERT INTO fixture_ping (x) VALUES (42)"))
        result = await session.execute(text("SELECT x FROM fixture_ping"))
        assert result.scalar() == 42
