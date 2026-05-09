"""commit_life_state_impl enqueues LifeStateChanged to outbox after a successful insert."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.life_dataflow import LifeStateChanged

CST = timezone(timedelta(hours=8))


@pytest.mark.asyncio
async def test_commit_life_state_emits_event(monkeypatch):
    from app.life.tool import commit_life_state_impl

    captured: list = []

    async def _fake_emit_tx(ev):
        captured.append(ev)

    @asynccontextmanager
    async def _fake_tx():
        yield

    async def _fake_insert(*_args, **_kwargs):
        return 12345
    monkeypatch.setattr("app.life.tool.insert_life_state", _fake_insert)

    monkeypatch.setattr("app.life.tool.tx", _fake_tx)
    monkeypatch.setattr("app.life.tool.emit_tx", _fake_emit_tx)

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
    assert len(captured) == 1
    ev = captured[0]
    assert isinstance(ev, LifeStateChanged)
    assert ev.persona_id == "p1"
    assert ev.activity_type == "browsing"
    assert ev.prev_activity_type == ""
