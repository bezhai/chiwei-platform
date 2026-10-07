"""查库被取消、超时之后的收尾有时限（2026-10-07，T9 决定 6）。

一条查询执行到一半被取消（外层超时、停进程、通道关闭打断了它），或者超过了连接上的时限，asyncpg
会另开一条连接通知数据库停下它，并等数据库关掉那条连接，这一步没有时限；SQLAlchemy 随后作废这条
连接时要先等它。数据库断开的消息传不回进程时（:mod:`tests.data.db_proxy`），任务就一直卡在收尾上、
占着连接池的一个位置，只有再被取消一次才出来。这里钉住：

* 外层超时、直接取消打断的查询：收尾最多再等 :data:`app.data.dialect.TERMINATE_GRACE_SECONDS`，
  任务带着原来的超时或取消结束，连接池的位置还回来；
* 超过语句时限的查询由数据库自己停下（:data:`app.data.session.STATEMENT_TIMEOUT_SECONDS`），
  不走通知数据库停下的那条路，马上带着数据库的报错结束；
* 数据库的回复传不回来、只能靠连接上的时限（:data:`app.data.session.COMMAND_TIMEOUT_SECONDS`）
  兜底时，同样在宽限期内结束，抛的还是超时，不会变成取消——上游把取消读成"在停"。

跑在真 Postgres 上，时限都缩短到一两秒。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.data import dialect
from app.data import session as session_mod
from app.data.session import get_session
from tests.data.db_proxy import process_db_behind

GRACE = 0.5
# 测试机上调度、收尾本身要花的时间。
SLACK = 1.0


@pytest.fixture
async def db(test_db, test_db_dsn, monkeypatch):
    monkeypatch.setattr(session_mod, "COMMAND_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(dialect, "TERMINATE_GRACE_SECONDS", GRACE)
    async with process_db_behind(test_db_dsn, monkeypatch) as (proxy, engine):
        yield proxy, engine


async def _finished_within(task: asyncio.Task, seconds: float) -> float:
    """等 ``task`` 结束，不去取消它（再取消一次就把它救出来了）；交回用了多久。"""
    started = time.monotonic()
    await asyncio.wait({task}, timeout=seconds + 5)
    took = time.monotonic() - started
    if not task.done():
        task.cancel()
        await asyncio.wait({task}, timeout=5)
        pytest.fail(f"still running after {seconds + 5:.1f}s: the cleanup hung")
    assert took <= seconds, f"took {took:.2f}s, more than {seconds:.2f}s"
    return took


async def test_a_query_cut_by_an_outer_timeout_ends_within_the_grace_as_that_timeout(db):
    _proxy, engine = db

    async def query_under_a_timeout():
        async with asyncio.timeout(0.3):
            async with get_session() as s:
                await s.execute(text("SELECT pg_sleep(5)"))

    task = asyncio.create_task(query_under_a_timeout())
    await _finished_within(task, 0.3 + GRACE + SLACK)

    assert isinstance(task.exception(), TimeoutError), repr(task.exception())
    assert engine.pool.checkedout() == 0


async def test_a_cancelled_query_ends_within_the_grace_as_cancelled(db):
    _proxy, engine = db

    async def query():
        async with get_session() as s:
            await s.execute(text("SELECT pg_sleep(5)"))

    task = asyncio.create_task(query())
    await asyncio.sleep(0.3)
    task.cancel()
    await _finished_within(task, GRACE + SLACK)

    assert task.cancelled()
    assert engine.pool.checkedout() == 0


async def test_a_statement_over_its_limit_is_stopped_by_the_database_itself(
    test_db, test_db_dsn, monkeypatch
):
    monkeypatch.setattr(session_mod, "COMMAND_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(dialect, "TERMINATE_GRACE_SECONDS", GRACE)
    # 语句时限在建连接时交给数据库，要在建连接池之前改。
    monkeypatch.setattr(session_mod, "STATEMENT_TIMEOUT_SECONDS", 0.3)

    async def slow_query():
        async with get_session() as s:
            await s.execute(text("SELECT pg_sleep(5)"))

    async with process_db_behind(test_db_dsn, monkeypatch) as (_proxy, engine):
        task = asyncio.create_task(slow_query())
        await _finished_within(task, 0.3 + SLACK)

        error = task.exception()
        assert isinstance(error, DBAPIError) and "statement timeout" in str(error), repr(error)
        assert engine.pool.checkedout() == 0


async def test_when_replies_stop_coming_the_connection_limit_ends_it_as_a_timeout(db):
    proxy, engine = db

    async def query_whose_reply_never_comes():
        async with get_session() as s:
            await s.execute(text("SELECT 1"))
            proxy.stall()
            await s.execute(text("SELECT 2"))

    task = asyncio.create_task(query_whose_reply_never_comes())
    await _finished_within(task, session_mod.COMMAND_TIMEOUT_SECONDS + GRACE + SLACK)

    assert isinstance(task.exception(), TimeoutError), repr(task.exception())
    assert engine.pool.checkedout() == 0
