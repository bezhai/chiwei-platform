"""每日底料的钟与落脚处 —— cron → 单字段 tick → 翻译补 lane → 眼睛节点（眼睛 Task 3）。

「fetch」概念已消解：认知层（看什么、怎么看、怎么叙述）整个在 :mod:`app.world.eyes`
（world 的感官器官——两层感知：本能扫视 + 有意张望），这里只剩**钟与落脚处**的接线：
把时间源信号翻译成带 lane 的执行信号、调眼睛、把眼睛带回的当日叙述落进
``DailyMaterials``。照搬 world heartbeat 的三层翻译解决框架硬约束「时间源的 Data
必须是单字段 ts」（否则源循环 ``_build_payload`` 填不了 lane → ValidationError 杀 Pod）：

  cron 0 4-23 * * * (Asia/Shanghai，白天每小时打点)
    → :class:`DailyMaterialsTick`（单字段 ts 的 transient tick，满足时间源约定）
    → :func:`fetch_to_materials_tick`（翻译节点，从 ``current_deployment_lane()`` 补 lane）
    → :class:`DailyMaterialsFetch`（带 lane 的执行信号）
    → :func:`daily_fetch_node`（单飞锁内：早退检查 → 眼睛 → 落库 → 记成本）

同日重试语义（感官器官配不上「一天一发、失败明天见」）：

  1. **单飞锁**：整段「早退检查 → 眼睛 → 落库」包在 ``single_flight``
     （Redis SETNX，跨进程）里，key 按 (lane, date)。幂等 claim 必须在 LLM
     之前——``insert_idempotent`` 只保最终只有一份数据、保不了只烧一次眼睛：
     同一钟点 tick 被 MQ 重复投递、或多个进程都挂了该 dataflow 时，锁外的
     早退检查会让两个并发执行都读到「今天还没有底料」、各烧一遍 agent
     token。锁冲突 = 持有方正在干活，静默 return
     （若持有方失败，下一钟点 cron 自然重试，不在这里 raise / 重试循环）。
  2. **早退（锁内）**：跑眼睛之前先查当天底料（:func:`find_daily_materials`）——已
     存在直接 return，不烧 agent token。检查必须在锁内：锁外检查仍有并发窗口。
  3. **失败不落库**：:func:`app.world.eyes.run_world_eyes` 抛错照实穿透、本轮啥也
     不落 → 下一钟点 cron 自动重试（眼睛 run 传 ``max_retries=1`` 关整轮重放，重试
     只留钟这一层）。
  4. **按天幂等**：落库走 ``insert_idempotent``（经 ``save_daily_materials``，第一份
     为准）——一天仍只有一份底料，数据层面的最后一道闸。

成本观测：record_round_cost(actor="world_eyes", round_id=当天日期)，失败 best-effort
吞掉（swallow 语义在 record_round_cost 里），不把一轮真实的看搞成失败。

dataflow 信号 kind（DailyMaterialsTick / DailyMaterialsFetch）**不改名**——MQ 遗留旧
schema 消息反序列化失败是踩过的 coe cutover 坑。wiring（cron 源 → tick → 翻译；
DailyMaterialsFetch 纯 in-process 接回本节点）在 ``app/wiring/fetch_dataflow.py`` 收口。
"""

from __future__ import annotations

import logging
from typing import Annotated

from app.agent.trace import collect_usage
from app.domain.thinking_cost import record_round_cost
from app.fetch.materials import find_daily_materials, save_daily_materials
from app.infra import cst_time
from app.runtime.data import Data, Key
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node
from app.runtime.single_flight import SingleFlightConflict, single_flight
from app.world.eyes import run_world_eyes  # module-level so tests can monkeypatch

logger = logging.getLogger(__name__)

# 眼睛单飞锁 TTL：要 > 一轮眼睛的最坏耗时（_EYES_RECURSION_LIMIT=10 轮模型调用、
# 每轮 LLM 几十秒 + 外部源 HTTP 工具，最坏 ~15min——world 单轮思考用 600s，眼睛
# 工具循环更长，定法同 WORLD_TICK_LOCK_TTL_SECONDS：比业务最坏耗时更大的上界）；
# 同时 < cron 钟点间隔 3600s——进程被杀没走到释放时，孤儿锁在下一钟点 tick 前
# 自然过期，不吃掉同日重试。TTL 到期后哪怕原 holder 还在跑、新 holder 也能进，
# token-CAS 释放保证不误删别人的锁（语义见 app/runtime/single_flight.py）。
WORLD_EYES_LOCK_TTL_SECONDS = 1800


class DailyMaterialsTick(Data):
    """每日底料的时间源信号——纯"到点了"，单字段 ``ts``。

    框架硬约定（runtime ``_build_payload``）：cron / interval 时间源每 tick 只
    用 ``data_type(ts=<iso>)`` 构造 payload，所以时间源的 Data 必须是带 ``ts: str``
    的单字段 tick（正例 :class:`app.world.engine.WorldHeartbeatTick`）。这个信号只
    决定"何时看"、也不需要 lane（lane 在翻译节点 :func:`fetch_to_materials_tick`
    按进程级泳道填），所以它干净地只有 ts。
    """

    ts: Annotated[str, Key]

    class Meta:
        transient = True


class DailyMaterialsFetch(Data):
    """带 lane 的执行信号（翻译节点 emit、in-process 接回 :func:`daily_fetch_node`）。

    transient——只当唤醒信号，底料内容在 durable ``DailyMaterials`` 表里。``lane`` 是
    必填非空 Key，整条链路的 lane 都由翻译节点这一处种下（落库的 lane 从这里传下去）。
    纯 in-process：``DailyMaterialsFetch`` 不直接挂时间源（时间源的单字段约束由
    :class:`DailyMaterialsTick` 承载），只承载翻译节点 emit 这一种来源。
    """

    lane: Annotated[str, Key]

    class Meta:
        transient = True


@node
async def fetch_to_materials_tick(_tick: DailyMaterialsTick) -> None:
    """把每钟点 cron 的单字段 ``DailyMaterialsTick`` 翻成带 lane 的 ``DailyMaterialsFetch``。

    这是时间源 → 眼睛节点的"变速箱"（照搬 :func:`app.world.engine.heartbeat_to_world_tick`）：
    时间源喂的单字段 ``DailyMaterialsTick`` 经这个机械翻译节点补上 lane，emit 一条
    ``DailyMaterialsFetch`` 经 in-process 边接回 :func:`daily_fetch_node`。

    lane 显式从**进程级部署泳道**取——cron 源循环的 context lane 是 ``None``（时间源
    不携带 request lane），所以这里不能靠 context 注入，必须自己读
    ``current_deployment_lane()``。prod（``LANE`` 未设 → None）归一到 ``"prod"``，与
    infra 各处 ``lane or "prod"`` 一致；``DailyMaterialsFetch.lane`` 是必填非空 Key。
    """
    await emit(DailyMaterialsFetch(lane=current_deployment_lane() or "prod"))


@node
async def daily_fetch_node(signal: DailyMaterialsFetch) -> None:
    """一轮眼睛执行：单飞锁内「早退检查 → 跑眼睛 → 落当日叙述 → 记成本」。

    0. 整段包进 ``single_flight``（key 按 lane+date）。幂等 claim 必须在 LLM 之前
       ——落库的 insert_idempotent 只保最终只有一份数据、保不了只烧一次眼睛：同一
       钟点 tick 被 MQ 重复投递 / 双进程同挂 dataflow 时，没有锁的话两个并发执行
       都会读到「今天还没有底料」、各烧一遍 agent token（数据不脏、token 白烧）。
       锁冲突 = 持有方正在干活，记 info 后静默 return（持有方失败的话下一钟点
       cron 自然重试，这里不 raise、不重试循环）。
    1. 算 CST「今天」；当天底料已存在直接早退（白天每小时打点的 cron 下，不再烧
       agent token——同日重试只补失败的天）。检查在锁内：锁外检查仍有并发窗口。
    2. 跑眼睛（认知层在 :func:`app.world.eyes.run_world_eyes`：读世界阶段 / 关注、拼
       两层感知 stimulus、agent 工具循环、返回当日叙述）。失败照实穿透：本轮不落
       库，下一钟点 cron 自动重试。collect_usage 包住整个认知层调用，截本轮累计
       token 落 durable PG（不依赖会系统性丢 trace 的 langfuse）。
    3. 落 DailyMaterials（按天幂等，insert_idempotent 第一份为准）。
    4. record_round_cost(actor="world_eyes", round_id=当天日期)——成本观测旁路，
       失败 best-effort 吞掉（swallow 语义在 record_round_cost 里）。
    """
    lane = signal.lane
    now = cst_time.now_cst()
    date = now.strftime("%Y-%m-%d")
    fetched_at = now.isoformat()

    try:
        async with single_flight(
            f"world_eyes:{lane}:{date}", ttl=WORLD_EYES_LOCK_TTL_SECONDS
        ):
            await _run_daily_fetch(lane=lane, date=date, fetched_at=fetched_at)
    except SingleFlightConflict:
        # 持有方正在烧这一天的眼睛；冗余唤醒静默让位（log 留痕、不抛）。若持有方
        # 失败不落库，下一钟点 cron 自然重试——不在这里 raise / 重试循环。
        logger.info(
            "[daily_fetch] %s %s 锁被持有（并发钟点 / 重复投递），让位给持有方",
            lane,
            date,
        )
        return


async def _run_daily_fetch(*, lane: str, date: str, fetched_at: str) -> None:
    """锁内的一轮眼睛编排：早退检查 → 眼睛 → 落库 → 记成本（已持有单飞锁）。"""
    # 早退（必须在锁内）：当天已有底料就不再调眼睛——锁保「只烧一次」，落库的
    # insert_idempotent 保「最终只有一份数据」，同日重试只补失败的天。
    if await find_daily_materials(lane=lane, date=date) is not None:
        logger.info(
            "[daily_fetch] %s %s 当天底料已存在，早退（同日重试只补失败的天）",
            lane,
            date,
        )
        return

    with collect_usage() as usage:
        briefing = await run_world_eyes(lane=lane, date=date)

    # 落 DailyMaterials（按天幂等）：只落眼睛组织好的那段当日叙述。
    await save_daily_materials(
        lane=lane,
        date=date,
        briefing=briefing,
        fetched_at=fetched_at,
    )

    # 本轮 token 落 durable PG（actor = "world_eyes"，与 world / world_reflect 同族
    # 区分），best-effort 吞掉失败：成本观测是旁路，绝不能因为记成本失败把一轮真实
    # 的看搞成失败（swallow 语义在 record_round_cost）。round_id 用当天日期（按天
    # 唯一、与按天幂等同源）。
    await record_round_cost(
        lane=lane,
        actor="world_eyes",
        round_id=date,
        usage=usage,
        observed_at=fetched_at,
    )

    logger.info("[daily_fetch] %s %s done", lane, date)
