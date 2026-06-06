"""WorldHeartbeatTick → WorldTick 翻译节点 —— 保底心跳源接法的命门.

时间源（interval）每 tick 只能喂单字段 ``WorldHeartbeatTick``（框架 _build_payload
的单字段 ts 约定）。这个翻译节点把它翻成 ``WorldTick(reason="heartbeat")``，并补上
lane —— **lane 必须从进程级部署泳道显式取**，因为 interval 源循环的 context lane
是 ``None``（时间源不携带 request lane），靠不上 context 注入。

这条 lane 注入是整条 world/life 回环的 lane 种子（world 叙述快照 / 信箱 / 动作的
分区键都从这里的 WorldTick.lane 一路传下去），所以单独锁死它的行为。
"""

from __future__ import annotations

import pytest

import app.world.engine as engine_mod
from app.world.engine import WorldHeartbeatTick, WorldTick, heartbeat_to_world_tick


def _capture_emit(monkeypatch):
    emitted: list = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(engine_mod, "emit", fake_emit)
    return emitted


@pytest.mark.asyncio
async def test_heartbeat_translated_to_world_tick_with_lane(monkeypatch):
    """部署泳道有值时，WorldTick.lane 从进程级部署泳道取。"""
    monkeypatch.setenv("LANE", "coe-hb")
    emitted = _capture_emit(monkeypatch)

    await heartbeat_to_world_tick(WorldHeartbeatTick(ts="2026-06-03T12:00:00Z"))

    assert len(emitted) == 1
    tick = emitted[0]
    assert isinstance(tick, WorldTick)
    assert tick.lane == "coe-hb"
    assert tick.reason == "heartbeat"
    # pull 范式：WorldTick 不再有 act_* 字段（act 不是唤醒源）
    assert not hasattr(tick, "act_id")


@pytest.mark.asyncio
async def test_heartbeat_lane_defaults_to_prod_when_lane_unset(monkeypatch):
    """生产里 LANE 未设（None）：WorldTick.lane 归一到非空 "prod"。

    WorldTick.lane 是必填非空 Key，生产 LANE 未设时若直接填 None 会构造失败。
    这条钉死 prod 默认值口径（与 infra 各处 ``lane or "prod"`` 一致）。
    """
    monkeypatch.delenv("LANE", raising=False)
    emitted = _capture_emit(monkeypatch)

    await heartbeat_to_world_tick(WorldHeartbeatTick(ts="2026-06-03T12:00:00Z"))

    assert len(emitted) == 1
    assert emitted[0].lane == "prod"
