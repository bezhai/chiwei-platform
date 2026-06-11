"""Cron-tick dataflow @node tests (light/heavy reviewer fan-out).

旧 life tick / glimpse / daily-plan 节点已在 world/life 重写中删除，对应测试
随之移除；voice 节点（fan_out_voice / voice_node）随 voice 子系统拆除删除。
这里只覆盖保留的 light / heavy 节点。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.domain.life_dataflow import (
    HeavyReviewRequest,
    HeavyReviewTick,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
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


def test_voice_nodes_gone():
    """voice 子系统拆除：fan_out_voice / voice_node 不得残留。"""
    import app.nodes.life_dataflow as ld

    for name in ("fan_out_voice", "voice_node", "_VOICE_TIMEOUT_S"):
        assert not hasattr(ld, name), f"{name} should have been deleted"


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
async def test_fan_out_light_skips_non_prod(reset_runtime, monkeypatch):
    """Non-prod lane → fan_out @node returns without emit."""
    monkeypatch.setattr("app.nodes.life_dataflow._is_prod", lambda: False)
    from app.nodes.life_dataflow import fan_out_light_day

    out = await fan_out_light_day(LightDayTick(ts="2026-04-30T08:00:00+08:00"))
    assert out is None


@pytest.mark.asyncio
async def test_fan_out_heavy(reset_runtime, mock_prod):
    from app.nodes.life_dataflow import fan_out_heavy

    out = await fan_out_heavy(HeavyReviewTick(ts="2026-04-30T03:00:00+08:00"))
    assert isinstance(out, HeavyReviewRequest)
