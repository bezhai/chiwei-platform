"""ActPerformed → ActWorldTick 翻译节点 — 阶段 1A（act 合并闸）.

life emit 的是 ``ActPerformed(lane, act_id, persona_id, description, occurred_at)``。
为了把"world 被唤醒最小间隔 1 分钟"做成 act→world 这条边上的合并闸，act 不再直接
打 ``WorldTick``，而是先翻成一个 transient ``ActWorldTick`` 走 60s debounce 合并闸
（短于 1min 的连续 act 合并成一次唤醒），闸后的节点再翻成 ``WorldTick(reason="act")``
唤醒 world 去推演客观结果。

这条边仍是 durable 跨进程（life 进程 → world 进程）：``wire(ActPerformed).
to(act_to_world_tick).durable()`` 承载、保留 ActPerformed 的 durable 幂等
（act_id 派生那套）。本测试只验翻译正确性（mock emit），durable + debounce
语义由 wiring 测试 / 集成测试覆盖。
"""

from __future__ import annotations

import pytest

import app.world.engine as engine_mod
from app.domain.world_events import ActPerformed
from app.world.engine import ActWorldTick, act_to_world_tick


@pytest.mark.asyncio
async def test_act_translated_to_act_world_tick(monkeypatch):
    """ActPerformed 的字段忠实翻进 transient ActWorldTick（进合并闸）。"""
    emitted: list = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(engine_mod, "emit", fake_emit)

    await act_to_world_tick(
        ActPerformed(
            lane="coe-t3",
            act_id="a1",
            persona_id="akao",
            description="我去厨房煮咖啡",
            occurred_at="2026-06-03T12:30:00Z",
        )
    )

    assert len(emitted) == 1
    wake = emitted[0]
    assert isinstance(wake, ActWorldTick)
    assert wake.lane == "coe-t3"
    assert wake.act_id == "a1"
    assert wake.act_persona_id == "akao"
    assert wake.act_description == "我去厨房煮咖啡"
    assert wake.act_occurred_at == "2026-06-03T12:30:00Z"
