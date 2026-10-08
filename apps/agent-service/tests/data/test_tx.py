"""Phase 7d Gap 13: tx() / current_session() / auto_tx().

Verifies the DB capability that hides ``AsyncSession`` from business code:

  - ``current_session()`` outside ``tx()`` raises (no implicit session).
  - Nested ``tx()`` uses SAVEPOINT — inner rollback leaves outer alive.
  - ``auto_tx()`` opens a one-shot tx, but reuses the existing one if any.
  - Concurrent branches each get their own session when they each enter ``tx()``
    from outside any tx (ContextVar isolation per asyncio task).
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from app.data.tx import auto_tx, current_session, tx

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
