"""Wiring: 每日外部底料抓取的 cron 链路（刀 3 Task2）。

Graph topology（照搬 world heartbeat 的三层翻译）:

  cron 0 5 * * * (Asia/Shanghai)
    -> DailyMaterialsTick     (单字段 ts 的 transient tick，满足时间源约定)
    -> fetch_to_materials_tick (翻译节点，从进程级泳道补 lane)
    -> DailyMaterialsFetch     (带 lane 的抓取信号)
    -> daily_fetch_node        (跑抓取 agent 自查 + 组织底料 → 落 DailyMaterials)

时间源不直接喂 DailyMaterialsFetch：那会在源循环 _build_payload(DailyMaterialsFetch(ts=...))
处 ValidationError 杀 Pod（DailyMaterialsFetch 无 ts、缺必填 lane）。所以中间隔一层
单字段 tick + 翻译节点补 lane（对称 WorldHeartbeatTick → heartbeat_to_world_tick）。

cron / interval 时间源在非 prod 泳道默认不跑（lane_policy.time_sources_enabled_by_default），
coe / ppe 验证抓取行为时用 DATAFLOW_ENABLE_TIME_SOURCES=1 显式打开。
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

# 每天凌晨 5:00 CST 触发一次抓取。cron 喂单字段 DailyMaterialsTick（满足时间源的
# 单字段 ts 约定），翻译节点 fetch_to_materials_tick 补上进程级泳道后 emit
# DailyMaterialsFetch 接回 daily_fetch_node。
wire(DailyMaterialsTick).from_(Source.cron("0 5 * * *", tz=TZ)).to(
    fetch_to_materials_tick
)
# DailyMaterialsFetch 退回纯 in-process：只承载翻译节点 emit 的抓取信号，打到抓取节点。
wire(DailyMaterialsFetch).to(daily_fetch_node)
