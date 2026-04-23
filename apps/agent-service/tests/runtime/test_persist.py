"""Append-only persistence integration tests.

每个测试用独立的 Data class 定义（避免跨测试污染 ``DATA_REGISTRY`` 的副作用），
在测试体最前面先用 ``plan_migration`` 把目标表建出来（fixture 每个测试 drop 全表，
所以每次都是从空库开始）。

覆盖：
  1. ``insert_append`` 对 ``Version`` 字段的自动递增（1,2,...）
  2. 并发 20 次写入时 advisory lock 保证 version 不冲突且仍然单调递增
  3. ``insert_idempotent`` 在相同 dedup_hash 上 ON CONFLICT DO NOTHING
     —— 第二次写入必须返回 0，并且历史行不被覆盖
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from app.runtime.data import Data, DedupKey, Key, Version
from app.runtime.persist import (
    insert_append,
    insert_idempotent,
    select_all_versions,
    select_latest,
)
from tests.runtime.conftest import migrate


class SAppend(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    mood: str


@pytest.mark.integration
async def test_insert_append_auto_versions(test_db):
    await migrate(SAppend, test_db)

    await insert_append(SAppend(pid="p1", mood="happy"))
    await insert_append(SAppend(pid="p1", mood="sad"))

    latest = await select_latest(SAppend, {"pid": "p1"})
    assert latest is not None
    assert latest.mood == "sad"

    all_rows = await select_all_versions(SAppend, {"pid": "p1"})
    assert [r.ver for r in all_rows] == [1, 2]


class SConcurrent(Data):
    pid: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    mood: str


@pytest.mark.integration
async def test_multi_replica_concurrency(test_db):
    """20 个并发 insert 不能撞 version。"""
    await migrate(SConcurrent, test_db)

    await asyncio.gather(
        *[insert_append(SConcurrent(pid="p1", mood=f"m{i}")) for i in range(20)]
    )

    rows = await select_all_versions(SConcurrent, {"pid": "p1"})
    assert len(rows) == 20
    versions = [r.ver for r in rows]
    assert versions == sorted(versions)
    assert len(set(versions)) == 20


class MIdempotent(Data):
    mid: Annotated[str, Key, DedupKey]
    gen: Annotated[int, DedupKey] = 0
    text: str


@pytest.mark.integration
async def test_insert_idempotent_on_conflict_do_nothing(test_db):
    await migrate(MIdempotent, test_db)

    n1 = await insert_idempotent(MIdempotent(mid="m1", text="first"))
    n2 = await insert_idempotent(MIdempotent(mid="m1", text="second"))

    assert n1 == 1
    assert n2 == 0

    rows = await select_all_versions(MIdempotent, {"mid": "m1"})
    assert len(rows) == 1
    assert rows[0].text == "first"
