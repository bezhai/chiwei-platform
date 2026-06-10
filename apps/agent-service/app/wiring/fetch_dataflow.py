"""Wiring: 每日底料的 cron 链路——眼睛的钟（眼睛 Task 3）。

Graph topology（照搬 world heartbeat 的三层翻译）:

  cron 0 4-23 * * * (Asia/Shanghai，白天每小时打点)
    -> DailyMaterialsTick      (单字段 ts 的 transient tick，满足时间源约定)
    -> fetch_to_materials_tick (翻译节点，从进程级泳道补 lane)
    -> DailyMaterialsFetch     (带 lane 的执行信号)
    -> daily_fetch_node        (早退检查 → 跑眼睛 → 落 DailyMaterials → 记成本)

04:00 第一班（角色醒来前底料就绪），之后每钟点打点做**同日重试**：某钟点眼睛失败
不落库、下一钟点自动再看；当天已成功则 node 早退（不烧 agent token）。按天幂等由
早退 + insert_idempotent 保证——一天仍只有一份底料，钟只是钟。

时间源不直接喂 DailyMaterialsFetch：那会在源循环 _build_payload(DailyMaterialsFetch(ts=...))
处 ValidationError 杀 Pod（DailyMaterialsFetch 无 ts、缺必填 lane）。所以中间隔一层
单字段 tick + 翻译节点补 lane（对称 WorldHeartbeatTick → heartbeat_to_world_tick）。

cron / interval 时间源在非 prod 泳道默认不跑（lane_policy.time_sources_enabled_by_default），
coe / ppe 验证眼睛行为时用 DATAFLOW_ENABLE_TIME_SOURCES=1 显式打开。
"""
from __future__ import annotations

from app.fetch.node import (
    DailyMaterialsFetch,
    DailyMaterialsTick,
    daily_fetch_node,
    fetch_to_materials_tick,
)
from app.runtime import Source, wire

TZ = "Asia/Shanghai"

# 白天每小时打点（04:00 第一班，失败下一钟点自动重试；当天已有底料由 node 早退）。
# cron 喂单字段 DailyMaterialsTick（满足时间源的单字段 ts 约定），翻译节点
# fetch_to_materials_tick 补上进程级泳道后 emit DailyMaterialsFetch 接回 daily_fetch_node。
wire(DailyMaterialsTick).from_(Source.cron("0 4-23 * * *", tz=TZ)).to(
    fetch_to_materials_tick
)
# DailyMaterialsFetch 纯 in-process：只承载翻译节点 emit 的执行信号，打到眼睛节点。
wire(DailyMaterialsFetch).to(daily_fetch_node)
