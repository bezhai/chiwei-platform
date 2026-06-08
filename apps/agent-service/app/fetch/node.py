"""通用抓取节点 —— cron → 单字段 tick → 翻译补 lane → 抓取节点（刀 3 Task2）。

每天凌晨（建议 5:00 CST）cron 触发一次抓取。照搬 world heartbeat 的三层翻译解决框架
硬约束「时间源的 Data 必须是单字段 ts」（否则源循环 ``_build_payload`` 填不了 lane →
ValidationError 杀 Pod）：

  cron 0 5 * * * (Asia/Shanghai)
    → :class:`DailyMaterialsTick`（单字段 ts 的 transient tick，满足时间源约定）
    → :func:`fetch_to_materials_tick`（翻译节点，从 ``current_deployment_lane()`` 补 lane）
    → :class:`DailyMaterialsFetch`（带 lane 的抓取信号）
    → :func:`daily_fetch_node`（真正抓取）

一轮抓取（:func:`daily_fetch_node`）回到**纯 agent 主导**：

  1. **直接跑抓取 agent**——node 不再先确定性调三个 skill。抓取 agent 拿着三个**结构化**
     查询 skill（query_weather / query_anime_calendar / query_holiday，各返回带 ``ok``
     的 dict）+ search_web 兜底，自己去查、自己看每个工具返回的 ``ok``、把真实数据组织
     成一段给世界引擎看的「今天的客观底料」中文话。某源 ``ok=false`` 时由 agent 的 prompt
     管它如实说没拿到、不编（见 :func:`app.fetch.agent.build_fetch_stimulus`）。
  2. **落 DailyMaterials**（按天幂等）：只落 agent 组织好的那段话（briefing）+ 抓取时刻。
  3. **成本** record_round_cost(actor="fetch", round_id=当天日期)。

失败语义命门：run 传 ``max_retries=1``——core 的 run 把整轮 ReAct 包在 ``@retry`` 里，
一次 model 调用瞬时失败会整轮重放、重放已执行的工具。关掉整轮重放后中途失败就抛、本轮
不落库，靠明天的 cron 兜底（按天幂等，今天没落明天不补，可接受——底料是按天的）。

框架原语：``Source.cron`` 定时、``emit`` 翻译投递、``insert_idempotent`` 落库（经
``save_daily_materials``）、``Agent.run`` agent 循环。本模块只用现成原语，不改 runtime /
core，import 但绝不改三个 skill / search_web / 框架。

wiring（cron 源 → DailyMaterialsTick → fetch_to_materials_tick；DailyMaterialsFetch
纯 in-process 接回 daily_fetch_node）在 ``app/wiring/fetch_dataflow.py`` 收口。
"""

from __future__ import annotations

import logging
from typing import Annotated

from app.agent.context import AgentContext
from app.agent.core import Agent
from app.agent.neutral import Message, Role
from app.agent.trace import collect_usage, make_session_id
from app.domain.thinking_cost import record_round_cost
from app.fetch.agent import FETCH_CFG, FETCH_TOOLS, build_fetch_stimulus
from app.fetch.materials import save_daily_materials
from app.infra import cst_time
from app.runtime.data import Data, Key
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.lane_policy import current_deployment_lane
from app.runtime.node import node

logger = logging.getLogger(__name__)


class DailyMaterialsTick(Data):
    """每日抓取的时间源信号——纯"到点了"，单字段 ``ts``。

    框架硬约定（runtime ``_build_payload``）：cron / interval 时间源每 tick 只
    用 ``data_type(ts=<iso>)`` 构造 payload，所以时间源的 Data 必须是带 ``ts: str``
    的单字段 tick（正例 :class:`app.world.engine.WorldHeartbeatTick`）。抓取信号只
    决定"何时抓"、也不需要 lane（lane 在翻译节点 :func:`fetch_to_materials_tick`
    按进程级泳道填），所以它干净地只有 ts。
    """

    ts: Annotated[str, Key]

    class Meta:
        transient = True


class DailyMaterialsFetch(Data):
    """带 lane 的抓取信号（翻译节点 emit、in-process 接回 :func:`daily_fetch_node`）。

    transient——只当唤醒信号，底料内容在 durable ``DailyMaterials`` 表里。``lane`` 是
    必填非空 Key，整条抓取链路的 lane 都由翻译节点这一处种下（落库的 lane 从这里传下去）。
    纯 in-process：``DailyMaterialsFetch`` 不直接挂时间源（时间源的单字段约束由
    :class:`DailyMaterialsTick` 承载），只承载翻译节点 emit 这一种来源。
    """

    lane: Annotated[str, Key]

    class Meta:
        transient = True


@node
async def fetch_to_materials_tick(_tick: DailyMaterialsTick) -> None:
    """把每日 cron 的单字段 ``DailyMaterialsTick`` 翻成带 lane 的 ``DailyMaterialsFetch``。

    这是时间源 → 抓取节点的"变速箱"（照搬 :func:`app.world.engine.heartbeat_to_world_tick`）：
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
    """一轮每日抓取（纯 agent 主导）：跑抓取 agent 自查 + 组织底料 → 落 briefing + 记成本。

    1. 算 CST「今天」+ 抓取时刻；构造只含「今天是哪天」的抓取意图 stimulus。
    2. 跑抓取 agent（offline-model + 三个结构化 skill + search_web）：agent 自己调那几个
       skill、看每个返回的 ``ok``、把真实数据组织成一段「今天的客观底料」中文话。run 传
       max_retries=1（关整轮重放）+ 按 (lane, "fetch", 今天) 的 session_id，用
       collect_usage 包住拿本轮累计 token。
    3. 落 DailyMaterials（按天幂等）：只落 agent 组织好的那段话 + 抓取时刻。
    4. record_round_cost(actor="fetch", round_id=今天日期)——成本观测旁路，失败 best-effort
       吞掉（swallow 语义在 record_round_cost 里），不把一轮真实抓取搞成失败。
    """
    lane = signal.lane
    now = cst_time.now_cst()
    date = now.strftime("%Y-%m-%d")
    fetched_at = now.isoformat()

    # round_id 用当天日期（按天唯一、与按天幂等同源）；session_id 按 (lane, "fetch", 今天)
    # 派生，这一天的抓取归一条 langfuse session。
    round_id = date
    session_id = make_session_id(lane, "fetch", date)
    stimulus = build_fetch_stimulus(date=date)
    context = AgentContext(session_id=session_id)

    # 纯 agent 主导：node 不预调 skill，工具集（三个结构化 skill + search_web）交给 agent
    # 自己调。max_retries=1 关掉整轮重放（durable 写不能被重放）；collect_usage 截本轮
    # token 落 durable PG（不依赖会系统性丢 trace 的 langfuse）。
    with collect_usage() as usage:
        result = await Agent(FETCH_CFG, tools=FETCH_TOOLS).run(
            messages=[Message(role=Role.USER, content=stimulus)],
            context=context,
            session_id=session_id,
            max_retries=1,
        )
    briefing = result.text()

    # 落 DailyMaterials（按天幂等）：只落 agent 组织好的那段话。
    await save_daily_materials(
        lane=lane,
        date=date,
        briefing=briefing,
        fetched_at=fetched_at,
    )

    # 本轮 token 落 durable PG（actor = "fetch"），best-effort 吞掉失败：成本观测是旁路，
    # 绝不能因为记成本失败把一轮真实抓取搞成失败（swallow 语义在 record_round_cost）。
    await record_round_cost(
        lane=lane,
        actor="fetch",
        round_id=round_id,
        usage=usage,
        observed_at=fetched_at,
    )

    logger.info("[daily_fetch] %s %s done", lane, date)
