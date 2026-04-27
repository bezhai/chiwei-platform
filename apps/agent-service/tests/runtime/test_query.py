"""Generic query builder integration tests.

覆盖 ``query(T).where(...).limit(...).order_by_desc(...).all()`` 的基本组合,
以及 builder 在误用场景下 fail-fast 的行为（未知列、重复 order_by、
Versioned Data 的 latest-per-key 模式禁用 order_by 等）。

``test_db`` 是 function-scoped，每个测试都要自己把表建出来并 seed 数据。
"""

from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, insert_idempotent
from app.runtime.query import query
from tests.runtime.conftest import migrate


class M(Data):
    mid: Annotated[str, Key]
    chat_id: str
    text: str


class V(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    mood: str


@pytest.mark.integration
async def test_query_where_limit(test_db):
    await migrate(M, test_db)

    await insert_idempotent(M(mid="m1", chat_id="c1", text="a"))
    await insert_idempotent(M(mid="m2", chat_id="c1", text="b"))
    await insert_idempotent(M(mid="m3", chat_id="c2", text="c"))

    rows = await query(M).where(chat_id="c1").all()
    assert len(rows) == 2

    rows = await query(M).where(chat_id="c1").limit(1).all()
    assert len(rows) == 1
    # LIMIT without order falls back to ORDER BY created_at; m1 was inserted
    # first, so we expect it rather than a random row from the planner.
    assert rows[0].mid == "m1"


@pytest.mark.integration
async def test_query_order_by_desc(test_db):
    # test_db 每个测试 drop 所有表，因此需要重新建表 + 重新 seed。
    await migrate(M, test_db)
    await insert_idempotent(M(mid="m1", chat_id="c1", text="a"))
    await insert_idempotent(M(mid="m2", chat_id="c1", text="b"))

    rows = await query(M).where(chat_id="c1").order_by_desc("mid").all()
    assert [r.mid for r in rows] == ["m2", "m1"]


@pytest.mark.integration
async def test_order_by_on_versioned_latest_rejected(test_db):
    """Versioned Data in latest-per-key mode must reject ``order_by_*``.

    DISTINCT ON 已经占据了 ORDER BY 位置，再加一段会出非法 SQL——builder
    应在 ``.all()`` 时主动拒绝，而不是把错误留给 Postgres。
    """
    await migrate(V, test_db)
    await insert_append(V(pid="p1", mood="happy"))

    with pytest.raises(ValueError, match="latest-per-key"):
        await query(V).where(pid="p1").order_by_desc("ver").all()


@pytest.mark.integration
async def test_order_by_on_versioned_all_versions_ok(test_db):
    """``.all_versions().order_by_*`` should work — full history sort."""
    await migrate(V, test_db)
    await insert_append(V(pid="p1", mood="a"))
    await insert_append(V(pid="p1", mood="b"))
    await insert_append(V(pid="p1", mood="c"))

    rows = (
        await query(V)
        .where(pid="p1")
        .all_versions()
        .order_by_desc("ver")
        .all()
    )
    assert [r.ver for r in rows] == [3, 2, 1]


@pytest.mark.integration
async def test_all_versions_default_order_by_ver(test_db):
    """``.all_versions()`` 无显式 order 时默认按 version ASC。

    对齐 ``select_all_versions`` 的语义：full history 需要稳定顺序，不能
    交给 planner。
    """
    await migrate(V, test_db)
    await insert_append(V(pid="p1", mood="a"))
    await insert_append(V(pid="p1", mood="b"))
    await insert_append(V(pid="p1", mood="c"))

    rows = await query(V).where(pid="p1").all_versions().all()
    assert [r.ver for r in rows] == [1, 2, 3]


def test_where_unknown_column_rejected():
    """Unknown column in ``.where(**kv)`` fails at call time."""
    with pytest.raises(ValueError, match="bogus"):
        query(M).where(bogus="x")


def test_order_by_unknown_column_rejected():
    """Unknown column in ``.order_by_*`` fails at call time."""
    with pytest.raises(ValueError, match="bogus"):
        query(M).order_by_desc("bogus")


def test_order_by_runtime_column_accepted():
    """``created_at`` is migrator-added but should be a valid order target."""
    # 只校验 builder 接受，不走 DB——避免为了 1 行断言起容器。
    q = query(M).order_by_desc("created_at")
    assert q._order == ("created_at", True)


def test_order_by_twice_rejected():
    """Re-setting the order silently drops the previous one — reject instead."""
    with pytest.raises(ValueError, match="only one ORDER BY"):
        query(M).order_by_desc("mid").order_by_desc("chat_id")

    with pytest.raises(ValueError, match="only one ORDER BY"):
        query(M).order_by_asc("mid").order_by_desc("mid")
