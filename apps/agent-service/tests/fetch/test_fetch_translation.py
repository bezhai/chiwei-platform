"""DailyMaterialsTick → DailyMaterialsFetch 翻译节点 —— cron 源接法的命门（刀 3 Task2）.

时间源（cron）每 tick 只能喂单字段 ``DailyMaterialsTick``（框架 _build_payload 的
单字段 ts 约定）。这个翻译节点把它翻成 ``DailyMaterialsFetch``，并补上 lane ——
**lane 必须从进程级部署泳道显式取**，因为 cron 源循环的 context lane 是 ``None``
（时间源不携带 request lane），靠不上 context 注入。照搬 world heartbeat 的三层翻译
（WorldHeartbeatTick → heartbeat_to_world_tick → WorldTick）。
"""

from __future__ import annotations

import app.fetch.node as fn
from app.fetch.node import (
    DailyMaterialsFetch,
    DailyMaterialsTick,
    fetch_to_materials_tick,
)


def _capture_emit(monkeypatch):
    emitted: list = []

    async def fake_emit(data):
        emitted.append(data)

    monkeypatch.setattr(fn, "emit", fake_emit)
    return emitted


def test_tick_is_single_field_ts():
    """时间源 Data 必须单字段 ts（否则源循环 _build_payload 填不了 lane → ValidationError 杀 Pod）。"""
    fields = set(DailyMaterialsTick.model_fields)
    assert fields == {"ts"}, f"DailyMaterialsTick 必须只有 ts 字段，实际 {fields}"


def test_tick_is_transient():
    """tick 是纯唤醒信号，transient（不落 pg）。"""
    assert getattr(DailyMaterialsTick.Meta, "transient", False) is True


async def test_translated_to_fetch_with_lane(monkeypatch):
    """部署泳道有值时，DailyMaterialsFetch.lane 从进程级部署泳道取。"""
    monkeypatch.setenv("LANE", "coe-fetch")
    emitted = _capture_emit(monkeypatch)

    await fetch_to_materials_tick(DailyMaterialsTick(ts="2026-06-08T06:00:00+08:00"))

    assert len(emitted) == 1
    out = emitted[0]
    assert isinstance(out, DailyMaterialsFetch)
    assert out.lane == "coe-fetch"


async def test_lane_defaults_to_prod_when_unset(monkeypatch):
    """生产里 LANE 未设（None）：DailyMaterialsFetch.lane 归一到非空 "prod"。

    DailyMaterialsFetch.lane 是必填非空 Key，生产 LANE 未设时若直接填 None 会构造失败。
    钉死 prod 默认值口径（与 infra 各处 ``lane or "prod"`` 一致）。
    """
    monkeypatch.delenv("LANE", raising=False)
    emitted = _capture_emit(monkeypatch)

    await fetch_to_materials_tick(DailyMaterialsTick(ts="2026-06-08T06:00:00+08:00"))

    assert len(emitted) == 1
    assert emitted[0].lane == "prod"
