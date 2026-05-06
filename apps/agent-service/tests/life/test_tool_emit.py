"""commit_life_state_impl emits LifeStateChanged after a successful insert."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.domain.life_dataflow import LifeStateChanged
from app.runtime import wire
from app.runtime.emit import reset_emit_runtime
from app.runtime.node import node
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring

CST = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def _reset():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


@pytest.mark.asyncio
async def test_commit_life_state_emits_event(monkeypatch):
    from app.life.tool import commit_life_state_impl

    seen: list[LifeStateChanged] = []

    async def _capture(c: LifeStateChanged) -> None:
        seen.append(c)
    wire(LifeStateChanged).to(node(_capture))

    # mock insert_life_state to skip db
    async def _fake_insert(*_args, **_kwargs):
        return 12345
    monkeypatch.setattr("app.life.tool.insert_life_state", _fake_insert)

    # mock get_session to a context manager that yields None
    class _NullSession:
        async def __aenter__(self): return None
        async def __aexit__(self, *_): return False
    monkeypatch.setattr("app.life.tool.get_session", lambda: _NullSession())

    now = datetime.now(CST)
    end = now + timedelta(hours=1)

    result = await commit_life_state_impl(
        persona_id="p1",
        activity_type="browsing",
        current_state="刷手机",
        response_mood="放松",
        state_end_at=end,
        skip_until=None,
        reasoning=None,
        now=now,
        prev_state=None,
    )
    assert result.ok is True
    assert len(seen) == 1
    assert seen[0].persona_id == "p1"
    assert seen[0].activity_type == "browsing"
    assert seen[0].prev_activity_type == ""


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_commit(monkeypatch, caplog):
    from app.life.tool import commit_life_state_impl

    async def _boom(c: LifeStateChanged) -> None:
        raise RuntimeError("downstream broken")
    wire(LifeStateChanged).to(node(_boom))

    async def _fake_insert(*_a, **_kw): return 1
    monkeypatch.setattr("app.life.tool.insert_life_state", _fake_insert)

    class _NullSession:
        async def __aenter__(self): return None
        async def __aexit__(self, *_): return False
    monkeypatch.setattr("app.life.tool.get_session", lambda: _NullSession())

    now = datetime.now(CST)
    result = await commit_life_state_impl(
        persona_id="p1",
        activity_type="browsing",
        current_state="x",
        response_mood="x",
        state_end_at=now + timedelta(hours=1),
        skip_until=None,
        reasoning=None,
        now=now,
        prev_state=None,
    )
    assert result.ok is True   # commit success despite emit failure
    assert "LifeStateChanged emit failed" in caplog.text
