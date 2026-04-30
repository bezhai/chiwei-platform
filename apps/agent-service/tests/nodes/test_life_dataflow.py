"""Phase 4 life_dataflow @node tests."""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.domain.life_dataflow import LifeTickRequest, MinuteTick, VoiceRequest
from app.runtime.emit import reset_emit_runtime
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring


CST = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def reset_runtime():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


@pytest.fixture
def mock_prod():
    with patch("app.nodes.life_dataflow._is_prod", return_value=True):
        yield


@pytest.fixture
def mock_personas(monkeypatch):
    async def _fake_list():
        return ["p1", "p2"]
    monkeypatch.setattr("app.nodes.life_dataflow._list_persona_ids", _fake_list)


@pytest.mark.asyncio
async def test_fan_out_life_tick_emits_per_persona(reset_runtime, mock_prod, mock_personas):
    from app.nodes.life_dataflow import fan_out_life_tick
    from app.runtime import wire
    from app.runtime.node import node

    seen: list[LifeTickRequest] = []

    async def _capture(r: LifeTickRequest) -> None:
        seen.append(r)
    probe = node(_capture)
    wire(LifeTickRequest).to(probe)

    await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert {r.persona_id for r in seen} == {"p1", "p2"}


@pytest.mark.asyncio
async def test_fan_out_life_tick_skips_non_prod(reset_runtime, mock_personas, monkeypatch):
    monkeypatch.setattr("app.nodes.life_dataflow._is_prod", lambda: False)
    from app.nodes.life_dataflow import fan_out_life_tick
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []

    async def _capture(r: LifeTickRequest) -> None:
        seen.append(r)
    wire(LifeTickRequest).to(node(_capture))

    await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_fan_out_life_tick_swallows_db_error(reset_runtime, mock_prod, monkeypatch, caplog):
    """DB 抖动不冒泡到 source loop。"""
    async def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("app.nodes.life_dataflow._list_persona_ids", _boom)

    from app.nodes.life_dataflow import fan_out_life_tick

    # No exception raised — only logged
    await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert "list_persona_ids failed" in caplog.text


@pytest.mark.asyncio
async def test_fan_out_voice_only_at_top_of_hour(reset_runtime, mock_prod, mock_personas):
    from app.nodes.life_dataflow import fan_out_voice
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: VoiceRequest) -> None: seen.append(r)
    wire(VoiceRequest).to(node(_capture))

    # 8:30 — wrong minute, no emit
    await fan_out_voice(MinuteTick(ts="2026-04-30T08:30:00+08:00"))
    assert seen == []

    # 8:00 — top of hour in 8..23, emits per persona
    await fan_out_voice(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert {r.persona_id for r in seen} == {"p1", "p2"}

    # 03:00 — top of hour but out of 8..23
    seen.clear()
    await fan_out_voice(MinuteTick(ts="2026-04-30T03:00:00+08:00"))
    assert seen == []
