"""Append-only persistence integration tests.

每个测试用独立的 Data class 定义（避免跨测试污染 ``DATA_REGISTRY`` 的副作用），
在测试体最前面先用 ``plan_migration`` 把目标表建出来（fixture 每个测试 drop 全表，
所以每次都是从空库开始）。

覆盖：
  1. ``insert_append`` 对 ``Version`` 字段的自动递增（1,2,...）
  2. 并发 20 次写入时 advisory lock 保证 version 不冲突且仍然单调递增
  3. ``insert_idempotent`` 在相同 dedup_hash 上 ON CONFLICT DO NOTHING
     —— 第二次写入必须返回 0，并且历史行不被覆盖
  4. 读的三个函数（``fetch_all`` / ``fetch_one`` / ``fetch_value``）：在调用方的 ``tx()`` 里
     就读那个事务（看得见它还没提交的写，不开 SAVEPOINT），不在事务里就自己开一个；
     ``from_row`` 把读出来的一行变成一条记录
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Annotated

import pytest
from sqlalchemy import text

from app.data.persist import (
    fetch_all,
    fetch_one,
    fetch_value,
    from_row,
    insert_append,
    insert_idempotent,
    select_latest,
)
from app.data.schema import Data, Key, Version, table_name
from app.data.tx import current_session, tx
from tests.data.read_back import all_versions
from tests.data.test_tx import savepoints
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

    all_rows = await all_versions(SAppend, {"pid": "p1"})
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

    rows = await all_versions(SConcurrent, {"pid": "p1"})
    assert len(rows) == 20
    versions = [r.ver for r in rows]
    assert versions == sorted(versions)
    assert len(set(versions)) == 20


class MIdempotent(Data):
    mid: Annotated[str, Key]
    text: str


@pytest.mark.integration
async def test_insert_idempotent_on_conflict_do_nothing(test_db):
    await migrate(MIdempotent, test_db)

    n1 = await insert_idempotent(MIdempotent(mid="m1", text="first"))
    n2 = await insert_idempotent(MIdempotent(mid="m1", text="second"))

    assert n1 == 1
    assert n2 == 0

    rows = await all_versions(MIdempotent, {"mid": "m1"})
    assert len(rows) == 1
    assert rows[0].text == "first"


# ---------------------------------------------------------------------------
# Reads: the SQL is the caller's, the transaction is storage's
# ---------------------------------------------------------------------------


class RNote(Data):
    """A row to read back: Key, Version and a timestamp column."""

    owner: Annotated[str, Key]
    note_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    body: str
    written_at: datetime


_RNOTES = table_name(RNote)


@pytest.fixture
async def notes_db(real_pg_required, test_db):
    await migrate(RNote, test_db)
    yield test_db


def _note(note_id: str, body: str, *, owner: str = "akao") -> RNote:
    return RNote(
        owner=owner,
        note_id=note_id,
        body=body,
        written_at=datetime.fromisoformat("2026-10-12T09:30:00+08:00"),
    )


async def _committed_notes(engine) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT count(*) FROM {_RNOTES}"))).scalar_one()


async def test_fetch_all_binds_the_params_by_name_and_keeps_the_order(notes_db):
    for note_id, body in (("n1", "first"), ("n2", "second"), ("n3", "third")):
        await insert_append(_note(note_id, body))
    await insert_append(_note("n9", "someone else's", owner="ayana"))

    rows = await fetch_all(
        f"SELECT * FROM {_RNOTES} WHERE owner = :owner ORDER BY note_id DESC", owner="akao"
    )

    assert [(r["note_id"], r["body"]) for r in rows] == [
        ("n3", "third"),
        ("n2", "second"),
        ("n1", "first"),
    ]
    assert await fetch_all(f"SELECT * FROM {_RNOTES} WHERE owner = :owner", owner="nobody") == []


async def test_fetch_one_is_the_first_row_or_none(notes_db):
    await insert_append(_note("n1", "first"))
    await insert_append(_note("n2", "second"))

    row = await fetch_one(f"SELECT * FROM {_RNOTES} ORDER BY note_id DESC")
    assert row is not None and row["note_id"] == "n2"
    assert await fetch_one(f"SELECT * FROM {_RNOTES} WHERE owner = :o", o="nobody") is None


async def test_fetch_value_is_the_first_column_of_the_first_row_or_none(notes_db):
    await insert_append(_note("n1", "first"))
    await insert_append(_note("n1", "again"))

    assert await fetch_value(f"SELECT MAX(ver) FROM {_RNOTES} WHERE note_id = :n", n="n1") == 2
    assert await fetch_value(f"SELECT body FROM {_RNOTES} WHERE note_id = :n", n="none") is None


async def test_a_read_outside_any_tx_runs_in_a_transaction_of_its_own(notes_db):
    await insert_append(_note("n1", "first"))

    with savepoints(notes_db) as seen:
        assert await fetch_value(f"SELECT count(*) FROM {_RNOTES}") == 1
        assert len(await fetch_all(f"SELECT * FROM {_RNOTES}")) == 1
        assert await fetch_one(f"SELECT * FROM {_RNOTES}") is not None

    assert seen == []
    with pytest.raises(RuntimeError, match="outside tx"):
        current_session()  # the read's own transaction is not left open


async def test_a_read_inside_tx_reads_that_transaction_and_opens_no_savepoint(notes_db):
    with savepoints(notes_db) as seen:
        async with tx():
            await insert_append(_note("n1", "not committed yet"))
            count = await fetch_value(f"SELECT count(*) FROM {_RNOTES}")
            rows = await fetch_all(f"SELECT body FROM {_RNOTES}")
            row = await fetch_one(f"SELECT body FROM {_RNOTES}")
            committed = await _committed_notes(notes_db)

    assert seen == [], f"a read inside tx() opened SAVEPOINT(s): {seen}"
    assert committed == 0, "the write had already committed: it was not in the caller's tx"
    assert count == 1, "the read did not see the caller's own uncommitted write"
    assert [r["body"] for r in rows] == ["not committed yet"]
    assert row is not None and row["body"] == "not committed yet"


async def test_from_row_turns_a_selected_row_into_the_record(notes_db):
    await insert_append(_note("n1", "first"))
    await insert_append(_note("n1", "second"))

    (row,) = await fetch_all(
        f"SELECT * FROM {_RNOTES} WHERE owner = :owner AND ver = :ver", owner="akao", ver=2
    )
    record = from_row(RNote, row)

    assert type(record) is RNote
    assert record == RNote(
        owner="akao",
        note_id="n1",
        ver=2,
        body="second",
        written_at=datetime.fromisoformat("2026-10-12T09:30:00+08:00"),
    )
    assert "dedup_hash" in row and "created_at" in row, (
        "SELECT * carries the storage columns; from_row takes only the record's fields"
    )


def test_from_row_needs_every_field_of_the_record():
    with pytest.raises(KeyError, match="written_at"):
        from_row(RNote, {"owner": "akao", "note_id": "n1", "ver": 1, "body": "first"})
