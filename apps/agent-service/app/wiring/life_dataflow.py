"""Wiring: world/life event 闭环.

Graph topology:

  interval 10min -> WorldHeartbeatTick -> heartbeat_to_world_tick -> WorldTick -> world_tick
  WorldTick (in-process: heartbeat / self-schedule) -> world_tick
  EventArrived .debounce() -> life_wake_node
  ScheduleReminderTick (in-process: 日程到点提醒回环) -> life_schedule_reminder_node

world-driven wake：角色被叫醒只剩 EventArrived 一条腿（world notify / 日程到点提醒 /
真人聊天都投信箱敲门走它）。角色的自排执行腿（LifeWakeTick -> life_self_wake_node）和
被否的 fan-out 定时心跳（LifeHeartbeatTick / LifeHeartbeatSweep）整套已拆掉——「到点
真把她叫起来」交给永远醒着的世界（world 每轮读 LifeState.next_wake_at 推演谁该叫）。

pull 范式：act 不再唤醒 world。life 做完一件事直接 insert_idempotent(ActPerformed)
落 PG，world 醒来按游标批量 pull——所以 ActPerformed 没有任何 wire。

旧 life tick / glimpse / schedule 生成的 wire 已在 world/life 重写中删除
（life_tick / glimpse / daily_plan / sync_life_state）；voice 的整条 cron 链
随 voice 子系统拆除删除；v4 记忆的 light/heavy review cron 线随旧记忆机器
整体删除。
"""
from __future__ import annotations

from app.domain.world_events import EventArrived, event_knock_key
from app.nodes.life_wake import (
    ScheduleReminderTick,
    life_schedule_reminder_node,
    life_wake_node,
)
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

# world-driven wake：角色的自排执行腿（LifeWakeTick → life_self_wake_node）和被否的
# fan-out 定时心跳（LifeHeartbeatTick / LifeHeartbeatSweep）整套已拆掉——唤醒只剩
# world notify 一条腿（走上面的 EventArrived → life_wake_node）。schedule 仍写下她想
# 几点醒的意愿（LifeState.next_wake_at），但「到点真把她叫起来」交给永远醒着的世界
# （world 每轮读 next_wake_at 推演谁过点了、用 notify 把她唤回来），不再有自排回环 wire。

# 日程到点提醒（备忘录 & 日程 第三块）：她 note / edit_note 排了带 remind_at 的日程 →
# 收口 fire_schedule_reminders 给每条各 emit_delayed(ScheduleReminderTick)，到期 emit
# 经这条纯 in-process 边接回 life_schedule_reminder_node（每条日程各挂各的、独立一路，
# **不动** next_wake_at 意愿语义）。节点走到点 gate（读 entry 最新一版判仍 active
# 且 remind_at 未改期 / 未撤）后 deliver_event 把这条投进她信箱、复用敲门把她叫醒。
# ScheduleReminderTick 是 transient（日程内容在 durable NotebookEntry 里），不挂时间源，
# 只承载到点提醒回环这一种来源。
wire(ScheduleReminderTick).to(life_schedule_reminder_node)
