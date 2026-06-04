"""IntentRaised → WorldTick 翻译节点 — stage3 联调收口.

life emit 的是 ``IntentRaised(lane, intent_id, persona_id, summary, occurred_at)``，
而 world_tick 入口要的是 ``WorldTick(reason="intent", intent_persona_id,
intent_summary)``。中间这个翻译节点把意图回灌翻成 world 的唤醒信号，让 world
被 reason="intent" 唤醒去裁决。

这条边是 durable 跨进程（life 进程 → world 进程）：life 起意写进 IntentRaised
信箱，world 端消费、翻成 WorldTick 打到 world_tick。本测试只验翻译正确性
（mock emit），durable 跨进程语义由 wiring 的 .durable() 承载、由集成测试覆盖。
"""

from __future__ import annotations

import pytest

import app.world.engine as engine_mod
from app.domain.world_events import IntentRaised
from app.world.engine import WorldTick, intent_to_world_tick


@pytest.mark.asyncio
async def test_intent_translated_to_world_tick(monkeypatch):
    """IntentRaised 的字段忠实翻进 WorldTick(reason="intent")。"""
    emitted: list = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(engine_mod, "emit", fake_emit)

    await intent_to_world_tick(
        IntentRaised(
            lane="coe-t3",
            intent_id="i1",
            persona_id="akao",
            summary="我想去厨房煮咖啡",
            occurred_at="2026-06-03T12:30:00Z",
        )
    )

    assert len(emitted) == 1
    tick = emitted[0]
    assert isinstance(tick, WorldTick)
    assert tick.lane == "coe-t3"
    assert tick.reason == "intent"
    assert tick.intent_persona_id == "akao"
    assert tick.intent_summary == "我想去厨房煮咖啡"
