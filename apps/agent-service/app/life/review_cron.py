"""睡前回顾的清晨对账主班 — 钟与对账节点（cron → 翻译 → 执行三层）.

主保证是钟（spec 决策 2）：快班靠她自己宣布入睡（sleep），但 sleep 声明可能整晚
不发生——部署丢在途自排消息、life 又没有保底心跳，一觉"没人看见"的日子不能就此
没有昨天页。所以清晨 05:00–10:00 cron 逐小时对**每个 persona** 对账「刚结束的
生活日」（04:00 晨界后整个上午都是前一日标签，窗口内每班 target 相同，见
:func:`app.life.living_day.previous_living_day`）。「那天回顾过没有」**看
data_day_page 该 (lane, persona, date) 的页是否存在**（:func:`app.life.pages.
day_page_exists`），不比对 LifeState 的单字段 marker——marker 会被清晨回笼觉的
快班推前到新生活日，误导对账班把已回顾的前一日重跑出重复页（2026-06-12 prod
事故根因）。逐小时窗口让 05:00 那班失败（模型抖动 / 部署窗口撞上）后
06:00–10:00 还有五班补，已有页的由页存在性挡住。钟纯机械、不依赖任何 agent
行为、部署杀不掉。

照 fetch_dataflow 的三层翻译解决框架硬约束「时间源的 Data 必须是单字段 ts」：

  cron 0 5-10 * * * (Asia/Shanghai)
    → :class:`LifeDayReviewTick`（单字段 ts 的 transient tick）
    → :func:`review_to_sweep_tick`（翻译节点，从进程级泳道补 lane）
    → :class:`LifeDayReviewSweep`（带 lane 的执行信号）
    → :func:`day_review_sweep_node`（逐 persona 对账 → run_day_review）

persona 清单从现成的 :func:`list_all_persona_ids`（bot_persona 表）取——
**不硬编三姐妹名字**（宪法：阵容是数据、不是代码）；只对**该 lane 有 LifeState
记录**的 persona 跑——bot_persona 全表里可能有没有 life 的 persona，对它们逐
小时空转对账语义不对。:func:`run_day_review` 自身 fail-open + single_flight +
锁内按页存在性权威复查（``trigger="sweep"``：已有页的日期绝不重跑）：一个
persona 失败绝不影响下一个；与快班撞车由锁解决（撞锁方静默让位）。

wiring（cron 源 → tick → 翻译；Sweep 纯 in-process 接回对账节点）在
``app/wiring/review_dataflow.py`` 收口。
"""

from __future__ import annotations

import logging
from typing import Annotated

from app.agent.trace import make_session_id
from app.data.queries.persona import (  # module-level so tests can monkeypatch
    list_all_persona_ids,
)
from app.domain.life_state import (  # module-level so tests can monkeypatch
    find_life_state,
)
from app.infra import cst_time
from app.life.living_day import previous_living_day
from app.life.pages import day_page_exists  # module-level so tests can monkeypatch
from app.life.review import run_day_review  # module-level so tests can monkeypatch
from app.runtime.data import Data, Key
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node

logger = logging.getLogger(__name__)


class LifeDayReviewTick(Data):
    """清晨对账班的时间源信号——纯"到点了"，单字段 ``ts``。

    框架硬约定（runtime ``_build_payload``）：cron 源每 tick 只用
    ``data_type(ts=<iso>)`` 构造 payload。lane 在翻译节点补，这里干净地只有 ts。
    """

    ts: Annotated[str, Key]

    class Meta:
        transient = True


class LifeDayReviewSweep(Data):
    """带 lane 的对账执行信号（翻译节点返回、in-process 接回对账节点）。

    transient——只当唤醒信号；回顾产出落在 DayPage / RelationshipPage 表里。
    ``lane`` 由翻译节点这一处种下（整条链路的泳道隔离从这里传下去）。
    """

    lane: Annotated[str, Key]

    class Meta:
        transient = True


@node
async def review_to_sweep_tick(_tick: LifeDayReviewTick) -> LifeDayReviewSweep:
    """把清晨 cron 的单字段 tick 翻成带 lane 的对账信号（时间源的"变速箱"）。

    lane 显式从进程级部署泳道取（cron 源循环的 context lane 是 None），prod
    （LANE 未设 → None）归一到 ``"prod"``——同 :func:`fetch_to_materials_tick`。
    """
    return LifeDayReviewSweep(lane=current_deployment_lane() or "prod")


@node
async def day_review_sweep_node(signal: LifeDayReviewSweep) -> None:
    """清晨对账班：对每个有 life 的 persona 检查刚结束的生活日，页缺失则补跑回顾。

    target = ``previous_living_day(now)``（04:00 晨界后整个上午都是前一日标签，
    05:00–10:00 窗口内每班 target 相同——失败的班下一小时补、已有页的由页存在
    性挡住）。该 lane 没有 LifeState 记录的 persona 过滤掉（没有生活日可对账，
    bot_persona 全表里可能有没有 life 的 persona）。「那天回顾过没有」按
    ``day_page_exists`` 查 data_day_page 该确切日期的页——绝不比对 LifeState 的
    单字段 marker（会被回笼觉的快班推前、误导重跑，2026-06-12 事故）。预检查是
    省一次锁的优化；权威复查在 ``run_day_review`` 锁内（trigger="sweep"）再做
    一次。run_day_review 绝不向上抛（fail-open），逐 persona 顺序对账互不拖累。
    """
    lane = signal.lane
    now = cst_time.now_cst()
    target_date = previous_living_day(now)
    today = now.strftime("%Y-%m-%d")

    persona_ids = await list_all_persona_ids()
    for persona_id in persona_ids:
        snapshot = await find_life_state(lane=lane, persona_id=persona_id)
        if snapshot is None:
            # 该 lane 没有这个 persona 的 LifeState（从没活过一轮）：没有生活日
            # 可对账，跳过——窗口逐小时对它空转查询虽便宜但语义不对。
            logger.debug(
                "[day_review_sweep] %s/%s no life state on this lane, skip",
                lane,
                persona_id,
            )
            continue
        if await day_page_exists(lane=lane, persona_id=persona_id, date=target_date):
            logger.info(
                "[day_review_sweep] %s/%s %s day page exists, skip",
                lane,
                persona_id,
                target_date,
            )
            continue
        await run_day_review(
            lane=lane,
            persona_id=persona_id,
            target_date=target_date,
            now=now,
            # langfuse 归组标签：persona 当天（自然日）的意识流 session（同快班
            # 把回顾 trace 归进她当天那条流；回顾自身无会话、不续接）。
            trace_session_id=make_session_id(lane, persona_id, today),
            trigger="sweep",
        )
