"""Phase 7b Gap 8: OutboxEmitter + transactional_emit."""
from __future__ import annotations

from typing import Annotated

import pytest
from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, Key
from app.runtime.outbox import transactional_emit

pytestmark = pytest.mark.integration


class _D(Data):
    id: Annotated[str, Key]
    val: str = ""


async def test_append_writes_pending_row(outbox_db: object) -> None:
    async with get_session() as s:
        async with transactional_emit(s) as emitter:
            await emitter.append(_D(id="x", val="v1"))
        await s.commit()
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT data_type, payload_json::text AS pj_text, state, origin_app FROM runtime_outbox"
        ))).mappings().all()
    assert len(rows) == 1
    assert rows[0]["state"] == "pending"
    assert rows[0]["data_type"].endswith("._D")
    assert "x" in rows[0]["pj_text"]
    assert rows[0]["origin_app"] in ("agent-service", "default")


async def test_session_rollback_drops_outbox_row(outbox_db: object) -> None:
    """Outbox row + business write are atomic — rollback drops both."""
    try:
        async with get_session() as s:
            async with transactional_emit(s) as emitter:
                await emitter.append(_D(id="rollback-me"))
            raise RuntimeError("simulate business error after append")
    except RuntimeError:
        pass
    async with get_session() as s:
        n = (await s.execute(text("SELECT count(*) FROM runtime_outbox"))).scalar()
    assert n == 0


async def test_lane_uses_current_lane_helper(outbox_db: object, monkeypatch) -> None:
    """Append must call current_lane(), not lane_var.get() directly."""
    monkeypatch.setenv("LANE", "feat-x")
    async with get_session() as s:
        async with transactional_emit(s) as emitter:
            await emitter.append(_D(id="lx"))
        await s.commit()
    async with get_session() as s:
        lane = (await s.execute(text(
            "SELECT lane FROM runtime_outbox WHERE payload_json->>'id'='lx'"
        ))).scalar()
    assert lane == "feat-x"


async def test_prod_lane_normalizes_to_null(outbox_db: object, monkeypatch) -> None:
    monkeypatch.delenv("LANE", raising=False)
    async with get_session() as s:
        async with transactional_emit(s) as emitter:
            await emitter.append(_D(id="prod"))
        await s.commit()
    async with get_session() as s:
        lane = (await s.execute(text(
            "SELECT lane FROM runtime_outbox WHERE payload_json->>'id'='prod'"
        ))).scalar()
    assert lane is None
