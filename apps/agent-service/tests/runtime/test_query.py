"""Generic query builder integration tests.

覆盖 ``query(T).where(...).limit(...).order_by_desc(...).all()`` 的基本组合。
``test_db`` 是 function-scoped，每个测试都要自己把表建出来并 seed 数据。
"""

from __future__ import annotations

from typing import Annotated

import pytest

from app.runtime.data import Data, Key
from app.runtime.persist import insert_idempotent
from app.runtime.query import query
from tests.runtime.conftest import migrate


class M(Data):
    mid: Annotated[str, Key]
    chat_id: str
    text: str


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


@pytest.mark.integration
async def test_query_order_by_desc(test_db):
    # test_db 每个测试 drop 所有表，因此需要重新建表 + 重新 seed。
    await migrate(M, test_db)
    await insert_idempotent(M(mid="m1", chat_id="c1", text="a"))
    await insert_idempotent(M(mid="m2", chat_id="c1", text="b"))

    rows = await query(M).where(chat_id="c1").order_by_desc("mid").all()
    assert [r.mid for r in rows] == ["m2", "m1"]
