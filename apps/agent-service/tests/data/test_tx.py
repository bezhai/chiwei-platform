"""Phase 7d Gap 13: tx() / current_session() / auto_tx().

Verifies the DB capability that hides ``AsyncSession`` from business code:

  - ``current_session()`` outside ``tx()`` raises (no implicit session).
  - Nested ``tx()`` uses SAVEPOINT — inner rollback leaves outer alive.
  - ``auto_tx()`` opens a one-shot tx, but reuses the existing one if any.
  - Concurrent branches each get their own session when they each enter ``tx()``
    from outside any tx (ContextVar isolation per asyncio task).
  - The storage functions (``app.data.persist``) join an open ``tx()`` without a
    SAVEPOINT, and open their own transaction when there is none.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

import pytest
from sqlalchemy import event, text

from app.data.persist import insert_append, insert_idempotent, select_latest
from app.data.schema import Data, Key, Version, table_name
from app.data.tx import auto_tx, current_session, tx
from tests.runtime.conftest import migrate

pytestmark = pytest.mark.integration


async def test_current_session_outside_tx_raises(test_db: object) -> None:
    with pytest.raises(RuntimeError, match="outside tx"):
        current_session()


async def test_tx_opens_session_and_current_session_works(test_db: object) -> None:
    async with tx():
        s = current_session()
        result = await s.execute(text("SELECT 1"))
        assert result.scalar() == 1


async def test_nested_tx_uses_savepoint_inner_rollback_only(test_db: object) -> None:
    """Inner raise rolls back inner SAVEPOINT only; outer still commits."""
    async with tx():
        s = current_session()
        await s.execute(
            text(
                "CREATE TEMP TABLE _probe ("
                "id TEXT PRIMARY KEY, tag TEXT NOT NULL"
                ")"
            )
        )
        await s.execute(
            text("INSERT INTO _probe (id, tag) VALUES (:i, :t)"),
            {"i": "outer", "t": "kept"},
        )

        with pytest.raises(RuntimeError, match="inner-boom"):
            async with tx():
                inner_s = current_session()
                # SAVEPOINT must reuse the outer session
                assert inner_s is s
                await inner_s.execute(
                    text("INSERT INTO _probe (id, tag) VALUES (:i, :t)"),
                    {"i": "inner", "t": "rolled-back"},
                )
                raise RuntimeError("inner-boom")

        # outer session still alive; inner row is gone, outer row remains
        ids = (
            await s.execute(text("SELECT id FROM _probe ORDER BY id"))
        ).scalars().all()
        assert ids == ["outer"]


async def test_auto_tx_outside_opens_oneshot_tx(test_db: object) -> None:
    async with auto_tx():
        s = current_session()
        assert (await s.execute(text("SELECT 42"))).scalar() == 42


async def test_auto_tx_inside_reuses_existing_session(test_db: object) -> None:
    async with tx():
        outer_s = current_session()
        async with auto_tx():
            inner_s = current_session()
            assert inner_s is outer_s


async def test_concurrent_branches_each_open_own_tx(test_db: object) -> None:
    """gather() called from outside any tx — each branch has its own session."""
    seen: dict[str, object] = {}

    async def branch(name: str, value: int) -> int:
        async with tx():
            s = current_session()
            seen[name] = s
            result = await s.execute(
                text("SELECT CAST(:v AS INTEGER)"), {"v": value}
            )
            return result.scalar()

    a, b = await asyncio.gather(branch("A", 100), branch("B", 200))
    assert a == 100
    assert b == 200
    assert seen["A"] is not seen["B"]


# ---------------------------------------------------------------------------
# The storage functions join the caller's transaction
#
# Several writes land together by running inside one ``tx()``; the storage
# functions join it through ``auto_tx()``. Joining must not open a SAVEPOINT:
# the replay records SAVEPOINTs as part of a transaction's writes and no
# baseline has one, so a storage function that nested ``tx()`` would change
# every transaction it ran in.
# ---------------------------------------------------------------------------


class TxNote(Data):
    """A versioned row, written with ``insert_append``."""

    note_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    body: str


class TxMark(Data):
    """A once-only row, written with ``insert_idempotent``."""

    mark_id: Annotated[str, Key]


@pytest.fixture
async def storage_db(real_pg_required, test_db):  # noqa: F811
    await migrate(TxNote, test_db)
    await migrate(TxMark, test_db)
    yield test_db


@contextmanager
def savepoints(engine) -> Iterator[list[str]]:
    """The names of the SAVEPOINTs opened on ``engine`` while the block runs."""
    seen: list[str] = []

    def on_savepoint(conn, name) -> None:
        seen.append(name)

    event.listen(engine.sync_engine, "savepoint", on_savepoint)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "savepoint", on_savepoint)


async def _committed(engine, cls: type[Data]) -> int:
    """How many rows of ``cls`` another connection sees: what has committed."""
    async with engine.connect() as conn:
        return (
            await conn.execute(text(f"SELECT count(*) FROM {table_name(cls)}"))
        ).scalar_one()


async def test_storage_writes_inside_tx_land_with_it_and_open_no_savepoint(storage_db):
    with savepoints(storage_db) as seen:
        async with tx():
            await insert_append(TxNote(note_id="n1", body="first"))
            await insert_append(TxNote(note_id="n1", body="second"))
            assert await insert_idempotent(TxMark(mark_id="m1")) == 1
            assert await insert_idempotent(TxMark(mark_id="m1")) == 0
            latest = await select_latest(TxNote, {"note_id": "n1"})
            assert await _committed(storage_db, TxNote) == 0, (
                "a write inside tx() is visible before the caller commits: it ran in a "
                "transaction of its own"
            )
            assert await _committed(storage_db, TxMark) == 0

    assert seen == [], f"joining the caller's tx() opened SAVEPOINT(s): {seen}"
    assert latest is not None
    assert (latest.ver, latest.body) == (2, "second"), "the read inside tx() saw its own writes"
    assert await _committed(storage_db, TxNote) == 2
    assert await _committed(storage_db, TxMark) == 1


async def test_storage_writes_inside_tx_roll_back_with_it(storage_db):
    with pytest.raises(RuntimeError, match="the caller failed"):
        async with tx():
            await insert_append(TxNote(note_id="n1", body="first"))
            await insert_idempotent(TxMark(mark_id="m1"))
            raise RuntimeError("the caller failed after both writes")

    assert await _committed(storage_db, TxNote) == 0
    assert await _committed(storage_db, TxMark) == 0
    assert await select_latest(TxNote, {"note_id": "n1"}) is None


async def test_a_storage_write_outside_any_tx_commits_on_its_own(storage_db):
    with savepoints(storage_db) as seen:
        assert await insert_append(TxNote(note_id="n1", body="first")) == 1
        assert await insert_idempotent(TxMark(mark_id="m1")) == 1

    assert seen == []
    assert await _committed(storage_db, TxNote) == 1
    assert await _committed(storage_db, TxMark) == 1
    with pytest.raises(RuntimeError, match="outside tx"):
        current_session()  # the write's own transaction is not left open
