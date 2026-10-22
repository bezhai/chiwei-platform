"""Smoke tests for the ``test_db`` fixture.

在 Task 0.8/0.9/0.11 之前先证明 fixture 本身工作：
  1. 能连上真实 Postgres，``SELECT 1`` 往返正常。
  2. 存储（:mod:`app.data.tx` 开的事务、:mod:`app.data.persist` 的读写）走测试容器
     （fixture 换掉的是 engine 和 session factory；写入不会影响生产库，跨测试表被清理）。
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
async def test_storage_routes_to_test_container(test_db):
    """存储开的事务必须走测试容器，不是 prod DSN。

    这个测试是 monkeypatch 的核心证据：如果 patch 没生效，
    ``tx()`` 会尝试连 prod（或失败，或写错库）。这里我们
    建一张临时表、写一行、读回来，只有走测试容器才能通；再从 fixture
    交出来的 engine 上读一遍，确认写进的就是这个容器。
    """
    from app.data.persist import fetch_value
    from app.data.tx import current_session, tx

    async with tx():
        await current_session().execute(
            text("CREATE TABLE IF NOT EXISTS fixture_ping (x INTEGER)")
        )
        await current_session().execute(text("INSERT INTO fixture_ping (x) VALUES (42)"))
    assert await fetch_value("SELECT x FROM fixture_ping") == 42
    async with test_db.connect() as conn:
        assert (await conn.execute(text("SELECT x FROM fixture_ping"))).scalar() == 42
