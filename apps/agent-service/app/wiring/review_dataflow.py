"""Wiring: 睡前回顾清晨对账班的 cron 链路——回顾的钟（主班）.

Graph topology（照 fetch_dataflow 的三层翻译）:

  cron 0 5-10 * * * (Asia/Shanghai，清晨对账窗口逐小时一班)
    -> LifeDayReviewTick      (单字段 ts 的 transient tick，满足时间源约定)
    -> review_to_sweep_tick   (翻译节点，从进程级泳道补 lane)
    -> LifeDayReviewSweep     (带 lane 的执行信号)
    -> day_review_sweep_node  (逐 persona 对账「刚结束的生活日」→ run_day_review)

05:00 = 生活日晨界（04:00）后一小时：对账的目标是「刚结束的生活日」（前一日
标签），给快班（她自己宣布入睡）留足当晚落 marker 的窗口。窗口逐小时到 10:00
（共六班）：05:00 那班失败（模型抖动 / 部署窗口撞上）还有五班补，已成功的由
marker 幂等挡住；previous_living_day 在 04:00 晨界后整个上午都是同一前一日
标签，窗口内每班 target 相同。主保证是钟：sleep 声明可能整晚不发生（部署丢
自排且 life 无心跳），快班只是体验加成。marker 已落（== 目标生活日）的
persona 由对账节点跳过；与快班撞车由回顾本体的 single_flight 解决。

时间源不直接喂 LifeDayReviewSweep：那会在源循环 _build_payload 处
ValidationError 杀 Pod（Sweep 无 ts、缺必填 lane），所以中间隔一层单字段 tick
+ 翻译节点补 lane（对称 DailyMaterialsTick → fetch_to_materials_tick）。

cron 时间源在非 prod 泳道默认不跑（lane_policy.time_sources_enabled_by_default），
coe / ppe 验证对账班行为时用 DATAFLOW_ENABLE_TIME_SOURCES=1 显式打开。
"""
from __future__ import annotations

from app.life.review_cron import (
    LifeDayReviewSweep,
    LifeDayReviewTick,
    day_review_sweep_node,
    review_to_sweep_tick,
)
from app.runtime import Source, wire

TZ = "Asia/Shanghai"

# 清晨 05:00–10:00 对账窗口逐小时一班（钟挂 wiring 层，节拍器不进业务）。cron 喂
# 单字段 LifeDayReviewTick，翻译节点补上进程级泳道后 emit LifeDayReviewSweep 接回
# 对账节点。已成功的班由 marker 幂等挡住，失败的班下一小时自动补。
wire(LifeDayReviewTick).from_(Source.cron("0 5-10 * * *", tz=TZ)).to(review_to_sweep_tick)
# LifeDayReviewSweep 纯 in-process：只承载翻译节点 emit 的执行信号。
wire(LifeDayReviewSweep).to(day_review_sweep_node)
