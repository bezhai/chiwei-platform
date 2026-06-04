"""Cron-tick dataflow @node tests (voice + light/heavy reviewer fan-out).

旧 life tick / glimpse / daily-plan 节点已在 world/life 重写中删除，对应测试
随之移除。这里只覆盖保留的 voice / light / heavy 节点。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.domain.life_dataflow import (
    HeavyReviewRequest,
    HeavyReviewTick,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
    MinuteTick,
    VoiceRequest,
)
from app.runtime.emit import reset_emit_runtime
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring


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
    """``_persona_dicts`` is called by the wire-level ``fan_out_per`` to fan a
    template Request into per-persona copies."""
    async def _fake_dicts():
        return [{"persona_id": "p1"}, {"persona_id": "p2"}]
    monkeypatch.setattr("app.nodes.life_dataflow._persona_dicts", _fake_dicts)


@pytest.mark.asyncio
async def test_fan_out_voice_only_at_top_of_hour(reset_runtime, mock_prod, mock_personas):
    from app.nodes.life_dataflow import _persona_dicts, fan_out_voice
    from app.runtime import wire
    from app.runtime.graph import compile_graph
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: VoiceRequest) -> None: seen.append(r)
    wire(VoiceRequest).fan_out_per(_persona_dicts).to(node(_capture))
    compile_graph()

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


@pytest.mark.asyncio
async def test_fan_out_voice_skips_non_prod(reset_runtime, mock_personas, monkeypatch):
    """Non-prod lane → fan_out @node returns without emit."""
    monkeypatch.setattr("app.nodes.life_dataflow._is_prod", lambda: False)
    from app.nodes.life_dataflow import _persona_dicts, fan_out_voice
    from app.runtime import wire
    from app.runtime.graph import compile_graph
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: VoiceRequest) -> None: seen.append(r)
    wire(VoiceRequest).fan_out_per(_persona_dicts).to(node(_capture))
    compile_graph()

    await fan_out_voice(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_fan_out_light_day_and_night_window_minutes(reset_runtime, mock_prod):
    from app.nodes.life_dataflow import fan_out_light_day, fan_out_light_night

    day = await fan_out_light_day(LightDayTick(ts="2026-04-30T08:00:00+08:00"))
    assert isinstance(day, LightReviewRequest)
    assert day.window_minutes == 30

    night = await fan_out_light_night(LightNightTick(ts="2026-04-30T23:00:00+08:00"))
    assert isinstance(night, LightReviewRequest)
    assert night.window_minutes == 60


@pytest.mark.asyncio
async def test_fan_out_heavy(reset_runtime, mock_prod):
    from app.nodes.life_dataflow import fan_out_heavy

    out = await fan_out_heavy(HeavyReviewTick(ts="2026-04-30T03:00:00+08:00"))
    assert isinstance(out, HeavyReviewRequest)


@pytest.mark.asyncio
async def test_voice_node_times_out(monkeypatch, caplog):
    import logging

    async def _hang(_persona_id: str):
        await asyncio.sleep(10)

    monkeypatch.setattr("app.memory.voice.generate_voice", _hang)
    monkeypatch.setattr("app.nodes.life_dataflow._VOICE_TIMEOUT_S", 0.01)

    from app.nodes.life_dataflow import voice_node

    with caplog.at_level(logging.ERROR):
        await voice_node(
            VoiceRequest(persona_id="p1", ts="2026-04-30T08:00:00+08:00")
        )

    assert "[p1] voice timed out" in caplog.text
