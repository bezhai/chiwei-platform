"""persona review 的每日补班 — 周级慢钟的钟（cron → 翻译 → sweep 三层）.

周级目标 + 每日补班（同睡前回顾对账班范式，spec 决策 4）：persona 慢漂一周一次，
但钟不敢只在周一打一班——那班失败（模型抖动 / 部署窗口撞上）这一周就空了。所以
每天 11:00 CST 一班（避开睡前回顾的 05:00–10:00 对账窗口），逐 persona 预检
「本周（自然周一 00:00 CST 起）有没有 source='review' 的新版本」
（:func:`app.life.persona_chain.has_review_version_this_week`，不靠单字段
marker——睡前回顾 marker 事故的教训），没有才进 :func:`run_persona_review`。
本周班成功后，余下六班由周级幂等挡住空转；失败 fail-open 次日自动补。

照 fetch_dataflow 的三层翻译解决框架硬约束「时间源的 Data 必须是单字段 ts」：

  cron 0 11 * * * (Asia/Shanghai)
    → :class:`PersonaReviewTick`（单字段 ts 的 transient tick）
    → :func:`persona_review_to_sweep_tick`（翻译节点，从进程级泳道补 lane）
    → :class:`PersonaReviewSweep`（带 lane 的执行信号）
    → :func:`persona_review_sweep_node`（逐 persona 预检 → run_persona_review）

persona 清单从现成的 :func:`list_all_persona_ids`（bot_persona 表）取——**不硬编
三姐妹名字**（宪法：阵容是数据、不是代码）。没活过的 persona 不在这里过滤：
:func:`run_persona_review` 自己的空窗口护栏（没有任何日页不烧模型）兜住，周级
的钟一天只问一次、空转查询便宜。run_persona_review 自身 fail-open +
single_flight + 锁内周级幂等复查：一个 persona 失败绝不影响下一个。

wiring（cron 源 → tick → 翻译；Sweep 纯 in-process 接回 sweep 节点）在
``app/wiring/persona_review_dataflow.py`` 收口。
"""

from __future__ import annotations

import logging
from typing import Annotated

from app.data.queries.persona import (  # module-level so tests can monkeypatch
    list_all_persona_ids,
)
from app.infra import cst_time
from app.life.persona_chain import (  # module-level so tests can monkeypatch
    has_review_version_this_week,
)
from app.life.persona_review import (  # module-level so tests can monkeypatch
    run_persona_review,
)
from app.runtime.data import Data, Key
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node

logger = logging.getLogger(__name__)


class PersonaReviewTick(Data):
    """每日补班的时间源信号——纯"到点了"，单字段 ``ts``。

    框架硬约定（runtime ``_build_payload``）：cron 源每 tick 只用
    ``data_type(ts=<iso>)`` 构造 payload。lane 在翻译节点补，这里干净地只有 ts。
    """

    ts: Annotated[str, Key]

    class Meta:
        transient = True


class PersonaReviewSweep(Data):
    """带 lane 的补班执行信号（翻译节点返回、in-process 接回 sweep 节点）。

    transient——只当唤醒信号；review 产出落在 PersonaVersion 版本链里。
    ``lane`` 由翻译节点这一处种下（整条链路的泳道隔离从这里传下去）。
    """

    lane: Annotated[str, Key]

    class Meta:
        transient = True


@node
async def persona_review_to_sweep_tick(
    _tick: PersonaReviewTick,
) -> PersonaReviewSweep:
    """把每日 cron 的单字段 tick 翻成带 lane 的补班信号（时间源的"变速箱"）。

    lane 显式从进程级部署泳道取（cron 源循环的 context lane 是 None），prod
    （LANE 未设 → None）归一到 ``"prod"``——同 :func:`review_to_sweep_tick`。
    """
    return PersonaReviewSweep(lane=current_deployment_lane() or "prod")


@node
async def persona_review_sweep_node(signal: PersonaReviewSweep) -> None:
    """每日补班：对每个 persona 预检本周周级幂等，没有 review 版才进 run。

    预检 :func:`has_review_version_this_week`（只认 source='review'——owner 盖版
    不挡班）是省一次锁的优化；权威复查在 ``run_persona_review`` 锁内再做一次。
    run_persona_review 绝不向上抛（fail-open），逐 persona 顺序补班互不拖累。
    """
    lane = signal.lane
    now = cst_time.now_cst()

    persona_ids = await list_all_persona_ids()
    for persona_id in persona_ids:
        if await has_review_version_this_week(
            lane=lane, persona_id=persona_id, now=now
        ):
            logger.info(
                "[persona_review_sweep] %s/%s already reviewed this week, skip",
                lane,
                persona_id,
            )
            continue
        await run_persona_review(lane=lane, persona_id=persona_id, now=now)
