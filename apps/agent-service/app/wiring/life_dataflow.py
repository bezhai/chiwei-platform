"""Wiring: voice + reviewer cron ticks + world/life event 闭环.

Graph topology:

  cron */1       -> MinuteTick     -> fan_out_voice
  cron 0,30 8-21 -> LightDayTick   -> fan_out_light_day
  cron 0 22-7 except 3 -> LightNightTick -> fan_out_light_night
  cron 0 3       -> HeavyReviewTick -> fan_out_heavy

  interval 10min -> WorldHeartbeatTick -> heartbeat_to_world_tick -> WorldTick -> world_tick
  WorldTick (in-process: heartbeat / self-schedule / act) -> world_tick
  ActPerformed .durable() -> act_to_world_tick -> ActWorldTick .debounce() -> world_act_wake -> WorldTick
  EventArrived .debounce() -> life_wake_node

旧 life tick / glimpse / schedule 生成的 wire 已在 world/life 重写中删除
（life_tick / glimpse / daily_plan / sync_life_state）。voice 与 light/heavy
reviewer 的 cron 保留，只是读状态口换成新 LifeState 主观快照。
"""
from __future__ import annotations

from app.domain.life_dataflow import (
    HeavyReviewRequest,
    HeavyReviewTick,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
    MinuteTick,
    VoiceRequest,
)
from app.domain.world_events import ActPerformed, EventArrived, event_knock_key
from app.nodes.life_dataflow import (
    _persona_dicts,
    fan_out_heavy,
    fan_out_light_day,
    fan_out_light_night,
    fan_out_voice,
    heavy_review_node,
    light_review_node,
    voice_node,
)
from app.nodes.life_wake import LifeWakeTick, life_self_wake_node, life_wake_node
from app.runtime import Source, wire
from app.world.engine import (
    WORLD_ACT_WAKE_DEBOUNCE_SECONDS,
    WORLD_ACT_WAKE_MAX_BUFFER,
    WORLD_HEARTBEAT_SECONDS,
    ActWorldTick,
    WorldHeartbeatTick,
    WorldTick,
    act_to_world_tick,
    act_wake_key,
    heartbeat_to_world_tick,
    world_act_wake,
    world_tick,
)

TZ = "Asia/Shanghai"

# world/life event 闭环攒批窗口：EventArrived 走 debounce 攒批唤醒 life，多条
# 积压只醒一次。窗口决定"何时醒 / 攒多久"，绝不进世界内容决策（spec key
# decision 2 的原语边界）。几秒窗口让同一轮里挤进来的多条 event 打成一批；
# max_buffer 只是防积压溢出的安全阀（攒够这么多条立即触发一次，不无限等）。
LIFE_WAKE_DEBOUNCE_SECONDS = 5
LIFE_WAKE_DEBOUNCE_MAX_BUFFER = 20

# Cron tick entry points — fan_out_xxx @node emits a per-persona-less
# template Request; the wire from that Request to the business node
# declares ``.fan_out_per(_persona_dicts)`` to expand it per persona
# with built-in failure isolation between personas.
wire(MinuteTick).from_(Source.cron("* * * * *", tz=TZ)).to(fan_out_voice)
wire(LightDayTick).from_(Source.cron("0,30 8-21 * * *", tz=TZ)).to(fan_out_light_day)
wire(LightNightTick).from_(Source.cron("0 22,23,0,1,2,4,5,6,7 * * *", tz=TZ)).to(fan_out_light_night)
wire(HeavyReviewTick).from_(Source.cron("0 3 * * *", tz=TZ)).to(fan_out_heavy)

# Per-persona business (declarative fan-out replaces hand-rolled
# ``_fan_out_per_persona`` loops; one persona failing does not abort
# the others — guaranteed by emit._dispatch_fan_out's
# asyncio.gather(return_exceptions=True)).
wire(VoiceRequest).fan_out_per(_persona_dicts).to(voice_node)
wire(LightReviewRequest).fan_out_per(_persona_dicts).to(light_review_node)
wire(HeavyReviewRequest).fan_out_per(_persona_dicts).to(heavy_review_node)

# ---------------------------------------------------------------------------
# world/life event 闭环 wiring。
# ---------------------------------------------------------------------------

# world 发动机三源同一入口（world_tick），但时间源不直接喂 WorldTick：
#   1) 保底心跳：interval 每 10 分钟喂一条单字段 WorldHeartbeatTick（满足框架
#      时间源的单字段 ts 约定），翻译节点 heartbeat_to_world_tick 补上进程级
#      泳道 + reason 后 emit WorldTick 接回 world_tick。WorldTick 直接挂时间源
#      会在源循环 _build_payload(WorldTick(ts=...)) 处 ValidationError 杀 Pod
#      （WorldTick 无 ts、且缺必填 lane），world 在生产里永远起不来。
#   2) 自排提前卡点：world_tick 内部 emit_delayed(WorldTick(reason="self"))，到期
#      emit(WorldTick) 经下面的 in-process 边接回 world_tick。
#   3) 动作回灌：world_act_wake emit 的 WorldTick(reason="act") 同样
#      经那条 in-process 边到 world_tick。
wire(WorldHeartbeatTick).from_(Source.interval(WORLD_HEARTBEAT_SECONDS)).to(
    heartbeat_to_world_tick
)
# WorldTick 退回纯 in-process：承载心跳翻译 / 自排 / 动作翻译三种来源 emit 的
# WorldTick，统一打到 world_tick。
wire(WorldTick).to(world_tick)

# life 回灌动作 → world 推演，中间夹一道 60s 合并闸（spec 决策 5：world 被唤醒
# 最小间隔 1min，短于 1min 的连续 act 合并成一次唤醒）。两段：
#   1) ActPerformed .durable() → act_to_world_tick：durable 跨进程（life 进程
#      做事写信箱，world 进程消费、翻成 transient ActWorldTick）。这条边原样
#      保留 durable —— ActPerformed 的 (lane,act_id) 自然键幂等不被破坏。
#   2) ActWorldTick .debounce(60s, per-lane) → world_act_wake：合并闸。同一
#      lane（= 一个 world）1min 窗口内的连续 act 合并成一次唤醒；闸后
#      world_act_wake 翻成 WorldTick(reason=act) 直接调 world_tick。world
#      撞锁时对 act 抛 SingleFlightConflict，world_act_wake 捕获后
#      raise DebounceReschedule 让闸重排（act 绝不丢）。
# 不能直接 debounce ActPerformed：它是 durable 持久化 Data（有 PG 表），而 debounce
# 的硬约束是 transient + 不可与 .durable() 组合，所以闸放在闸后的 transient 信号上。
wire(ActPerformed).to(act_to_world_tick).durable()
wire(ActWorldTick).debounce(
    seconds=WORLD_ACT_WAKE_DEBOUNCE_SECONDS,
    max_buffer=WORLD_ACT_WAKE_MAX_BUFFER,
    key_by=act_wake_key,
).to(world_act_wake)

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
