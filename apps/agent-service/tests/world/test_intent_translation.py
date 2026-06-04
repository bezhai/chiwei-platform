"""IntentRaised → IntentWorldTick 翻译节点 — Task 2（降频：intent 合并闸）.

life emit 的是 ``IntentRaised(lane, intent_id, persona_id, summary, occurred_at)``。
为了把"world 被唤醒最小间隔 1 分钟"做成 intent→world 这条边上的合并闸，intent
不再直接打 ``WorldTick``，而是先翻成一个 transient ``IntentWorldTick`` 走 60s
debounce 合并闸（短于 1min 的连续 intent 合并成一次唤醒），闸后的节点再翻成
``WorldTick(reason="intent")`` 唤醒 world 裁决。

这条边仍是 durable 跨进程（life 进程 → world 进程）：``wire(IntentRaised).
to(intent_to_world_tick).durable()`` 承载、保留 IntentRaised 的 durable 幂等
（intent_id 派生那套）。本测试只验翻译正确性（mock emit），durable + debounce
语义由 wiring 测试 / 集成测试覆盖。
"""

from __future__ import annotations

import pytest

import app.world.engine as engine_mod
from app.domain.world_events import IntentRaised
from app.world.engine import IntentWorldTick, intent_to_world_tick


@pytest.mark.asyncio
async def test_intent_translated_to_intent_world_tick(monkeypatch):
    """IntentRaised 的字段忠实翻进 transient IntentWorldTick（进合并闸）。"""
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
    wake = emitted[0]
    assert isinstance(wake, IntentWorldTick)
    assert wake.lane == "coe-t3"
    assert wake.intent_id == "i1"
    assert wake.intent_persona_id == "akao"
    assert wake.intent_summary == "我想去厨房煮咖啡"
