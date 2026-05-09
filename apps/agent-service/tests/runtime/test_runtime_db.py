"""Phase 7d Gap 13: tx() / current_session() / emit_tx() / auto_tx().

Verifies the DB capability that hides ``AsyncSession`` from business code:

  - ``current_session()`` outside ``tx()`` raises (no implicit session).
  - ``emit_tx()`` outside ``tx()`` raises (outbox MUST be atomic with writes).
  - Nested ``tx()`` uses SAVEPOINT — inner rollback leaves outer alive.
  - ``auto_tx()`` opens a one-shot tx, but reuses the existing one if any.
  - Concurrent branches each get their own session when they each enter ``tx()``
    from outside any tx (ContextVar isolation per asyncio task).
"""
from __future__ import annotations

import asyncio
from typing import Annotated

import pytest
from sqlalchemy import text

from app.runtime.data import Data, Key
from app.runtime.db import auto_tx, current_session, emit_tx, tx

pytestmark = pytest.mark.integration


class _ProbeData(Data):
    id: Annotated[str, Key]
    val: str = ""


async def test_current_session_outside_tx_raises(test_db: object) -> None:
    with pytest.raises(RuntimeError, match="outside tx"):
        current_session()


async def test_tx_opens_session_and_current_session_works(test_db: object) -> None:
    async with tx():
        s = current_session()
        result = await s.execute(text("SELECT 1"))
        assert result.scalar() == 1


async def test_emit_tx_outside_tx_raises(outbox_db: object) -> None:
    with pytest.raises(RuntimeError, match="outside tx"):
        await emit_tx(_ProbeData(id="never-written"))


async def test_emit_tx_inside_tx_writes_outbox_row(outbox_db: object) -> None:
    async with tx():
        await emit_tx(_ProbeData(id="in-tx", val="v1"))
        s = current_session()
        rows = (
            await s.execute(
                text(
                    "SELECT data_type, payload_json::text AS pj, state "
                    "FROM runtime_outbox WHERE payload_json->>'id'='in-tx'"
                )
            )
        ).mappings().all()
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["data_type"].endswith("._ProbeData")
    assert "v1" in rows[0]["pj"]


async def test_nested_tx_uses_savepoint_inner_rollback_only(outbox_db: object) -> None:
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


async def test_auto_tx_outside_opens_oneshot_tx(outbox_db: object) -> None:
    async with auto_tx():
        s = current_session()
        assert (await s.execute(text("SELECT 42"))).scalar() == 42


async def test_auto_tx_inside_reuses_existing_session(outbox_db: object) -> None:
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
