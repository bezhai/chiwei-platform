"""Wiring: world/life event 闭环.

Graph topology:

  interval 10min -> WorldHeartbeatTick -> heartbeat_to_world_tick -> WorldTick -> world_tick
  WorldTick (in-process: heartbeat / self-schedule) -> world_tick
  EventArrived .debounce() -> life_wake_node
  LifeWakeTick (in-process: life 自排回环) -> life_self_wake_node

pull 范式：act 不再唤醒 world。life 做完一件事直接 insert_idempotent(ActPerformed)
落 PG，world 醒来按游标批量 pull——所以 ActPerformed 没有任何 wire。

旧 life tick / glimpse / schedule 生成的 wire 已在 world/life 重写中删除
（life_tick / glimpse / daily_plan / sync_life_state）；voice 的整条 cron 链
随 voice 子系统拆除删除；v4 记忆的 light/heavy review cron 线随旧记忆机器
整体删除。
"""
from __future__ import annotations

from app.domain.world_events import EventArrived, event_knock_key
from app.nodes.life_wake import LifeWakeTick, life_self_wake_node, life_wake_node
from app.runtime import Source, wire
from app.world.engine import (
    WORLD_HEARTBEAT_SECONDS,
    WorldHeartbeatTick,
    WorldTick,
    heartbeat_to_world_tick,
    world_tick,
)

# world/life event 闭环攒批窗口：EventArrived 走 debounce 攒批唤醒 life，多条
# 积压只醒一次。窗口决定"何时醒 / 攒多久"，绝不进世界内容决策（spec key
# decision 2 的原语边界）。几秒窗口让同一轮里挤进来的多条 event 打成一批；
# max_buffer 只是防积压溢出的安全阀（攒够这么多条立即触发一次，不无限等）。
LIFE_WAKE_DEBOUNCE_SECONDS = 5
LIFE_WAKE_DEBOUNCE_MAX_BUFFER = 20

# world 发动机两源同一入口（world_tick），但时间源不直接喂 WorldTick：
#   1) 保底心跳：interval 每 10 分钟喂一条单字段 WorldHeartbeatTick（满足框架
#      时间源的单字段 ts 约定），翻译节点 heartbeat_to_world_tick 补上进程级
#      泳道 + reason 后 emit WorldTick 接回 world_tick。WorldTick 直接挂时间源
#      会在源循环 _build_payload(WorldTick(ts=...)) 处 ValidationError 杀 Pod
#      （WorldTick 无 ts、且缺必填 lane），world 在生产里永远起不来。
#   2) 自排提前卡点（主节奏）：world_tick 内部 emit_delayed(WorldTick(reason="self"))，
#      到期 emit(WorldTick) 经下面的 in-process 边接回 world_tick。
wire(WorldHeartbeatTick).from_(Source.interval(WORLD_HEARTBEAT_SECONDS)).to(
    heartbeat_to_world_tick
)
# WorldTick 退回纯 in-process：承载心跳翻译 / 自排两种来源 emit 的 WorldTick，
# 统一打到 world_tick。
wire(WorldTick).to(world_tick)

# pull 范式：act 不再唤醒 world。life 做完一件事在 perform_act 里直接
# insert_idempotent(ActPerformed) 落 PG（不 emit、不走 RabbitMQ、不触发唤醒），
# world 醒来按游标批量 pull list_recent_acts。所以 ActPerformed 没有任何 wire——
# 频率主权完全交回 world 自己的 sleep，不再被每条 act 拽起来全量推演。

# 信箱来新 event → debounce 攒批唤醒对应 life（同构跑三姐妹，persona 由
# EventArrived 决定）。key_by 复用 event_knock_key，每个 (lane, persona) 自己
# 攒批，互不干扰，与信箱隔离口径一致。
wire(EventArrived).debounce(
    seconds=LIFE_WAKE_DEBOUNCE_SECONDS,
    max_buffer=LIFE_WAKE_DEBOUNCE_MAX_BUFFER,
    key_by=event_knock_key,
).to(life_wake_node)

# life 自排唤醒（阶段 1B Task 2）：她调 schedule 自排 → 收口
# emit_delayed(LifeWakeTick(reason="self"))，到期 emit(LifeWakeTick) 经这条纯
# in-process 边接回 life_self_wake_node（对称 world 的 self WorldTick 回环）。
# **独立信号、不复用 EventArrived 通道**（spec decision 6）：self 唤醒入口走到点
# gate + 空信箱也跑一轮，与信箱敲门的 life_wake_node 是两条独立路径。LifeWakeTick
# 是 transient，不挂时间源（life 没有独立保底心跳），只承载自排回环这一种来源。
wire(LifeWakeTick).to(life_self_wake_node)
