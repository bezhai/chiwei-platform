"""Wiring: persona review 每日补班的 cron 链路——周级慢钟的钟.

Graph topology（照 fetch_dataflow / review_dataflow 的三层翻译）:

  cron 0 11 * * * (Asia/Shanghai，每天一班的补班)
    -> PersonaReviewTick            (单字段 ts 的 transient tick，满足时间源约定)
    -> persona_review_to_sweep_tick (翻译节点，从进程级泳道补 lane)
    -> PersonaReviewSweep           (带 lane 的执行信号)
    -> persona_review_sweep_node    (逐 persona 预检周级幂等 → run_persona_review)

11:00 = 避开睡前回顾的清晨 05:00–10:00 对账窗口（两把钟不抢同一段时间的模型与
锁），也躲开生活日晨界（04:00）附近的语义模糊。persona 慢漂是**周级目标 + 每日
补班**（spec 决策 4）：本周班成功后余下六班由周级幂等
（has_review_version_this_week，只认 source='review'）挡住空转，失败的班次日
自动补。钟纯机械、不依赖任何 agent 行为、部署杀不掉。

时间源不直接喂 PersonaReviewSweep：那会在源循环 _build_payload 处
ValidationError 杀 Pod（Sweep 无 ts、缺必填 lane），所以中间隔一层单字段 tick
+ 翻译节点补 lane（对称 LifeDayReviewTick → review_to_sweep_tick）。

cron 时间源在非 prod 泳道默认不跑（lane_policy.time_sources_enabled_by_default），
coe / ppe 验证补班行为时用 DATAFLOW_ENABLE_TIME_SOURCES=1 显式打开。
"""
from __future__ import annotations

from app.life.persona_review_cron import (
    PersonaReviewSweep,
    PersonaReviewTick,
    persona_review_sweep_node,
    persona_review_to_sweep_tick,
)
from app.runtime import Source, wire

TZ = "Asia/Shanghai"

# 每天 11:00 一班的补班（钟挂 wiring 层，节拍器不进业务）。cron 喂单字段
# PersonaReviewTick，翻译节点补上进程级泳道后 emit PersonaReviewSweep 接回
# sweep 节点。本周已成功的班由周级幂等挡住，失败的班次日自动补。
#
# ############################################################################
# TEMP: 泳道验证脚手架，**验证完必须 revert，禁止合进 main**。
#
# 生产值是 "0 11 * * *"（每天 11:00 CST 一班）。这里临时提频到每 5 分钟一班，
# 只为在 coe 泳道当天已过 11:00 的情况下不等自然触发就能跑到 persona_review
# 的真实调用（本批要验的是 prompt_vars 新增 persona_core 的契约）。
#
# 周级幂等（has_review_version_this_week，只认 source='review'，周界 = 自然周一
# 00:00 CST）没动，所以提频后在该 lane 上依然只会真正跑一次：版本链为空 →
# seed v0 → review v1 → 本周后续班全部被幂等挡住空转。这正是我们要的（一次
# 真实调用），不要为了多跑几次去动幂等——那会掩盖真实行为。
#
# 同步改了 tests/wiring/test_persona_review_dataflow_wiring.py 的断言值（那里
# 也有对应的 TEMP 注释），revert 时两处一起回来。
# ############################################################################
wire(PersonaReviewTick).from_(Source.cron("*/5 * * * *", tz=TZ)).to(
    persona_review_to_sweep_tick
)
# PersonaReviewSweep 纯 in-process：只承载翻译节点 emit 的执行信号。
wire(PersonaReviewSweep).to(persona_review_sweep_node)
