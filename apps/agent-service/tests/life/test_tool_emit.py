"""commit_life_state_impl enqueues LifeStateChanged to outbox after a successful insert."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.life_dataflow import LifeStateChanged

CST = timezone(timedelta(hours=8))


@pytest.mark.asyncio
async def test_commit_life_state_emits_event(monkeypatch):
    from app.life.tool import commit_life_state_impl

    captured: list = []

    @asynccontextmanager
    async def _fake_te(_session):
        emitter = MagicMock()
        emitter.append = AsyncMock(side_effect=lambda ev: captured.append(ev) or None)
        yield emitter

    async def _fake_insert(*_args, **_kwargs):
        return 12345
    monkeypatch.setattr("app.life.tool.insert_life_state", _fake_insert)

    class _SessionCtx:
        async def __aenter__(self): return AsyncMock()
        async def __aexit__(self, *_): return False
    monkeypatch.setattr("app.life.tool.get_session", lambda: _SessionCtx())
    monkeypatch.setattr("app.life.tool.transactional_emit", _fake_te)

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
