"""world 的 agent 怎么调模型：主 agent 和三类按需调用的 agent（感知判断、NPC、应答）共用这一处。

每一类是一个 :class:`AgentKind`：自己的 Langfuse prompt、自己的 trace 名、自己的模型配置键。
四类都经 :func:`run_agent` 调一次模型（工具循环跑到它不再调工具为止）：

* **用哪个模型**走 Dynamic Config：每一类一个键（``model_key``）；没配就跟主 agent 用同一个
  （:data:`WORLD_MODEL_KEY`），那个也没配就是 :data:`DEFAULT_WORLD_MODEL`。所以一开始四类用
  同一个模型，哪一类需要换（应答要快，扮演 NPC 要会写）再单独配，不用改代码。
* **一次调用最多几次带工具的模型调用**四类共用 :data:`WORLD_RECURSION_LIMIT_KEY`。它只是让一次
  调用停得下来的上限，不决定 agent 做什么；撞上它时框架再给一次不带工具的调用收尾。
* **每一次调用是一条单独的 trace**（:func:`app.agent.trace.separate_trace`），trace 名就是这一类
  的 ``trace_name``。感知判断和 NPC 是在主 agent 的工具里调的，不另起一条就会挂进主 agent 那条
  trace，还会把它的名字改掉。四类都归到同一个 langfuse session（:func:`session_key`），按时间
  顺着看。
* **成本**每一次调用记一行（:func:`app.domain.thinking_cost.record_round_cost`）：actor 是
  ``world``，round_id 是 ``<trace 名>:<调用 id>``，按前缀就分得出四类各花了多少。

:func:`build_runner` 是测试换替身的地方：四类都经它拿 runner，测试从这里换掉，不碰真模型。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from inner_shared.dynamic_config import dynamic_config

from app.agent.context import AgentContext
from app.agent.core import AgentConfig
from app.agent.neutral import Message as Turn
from app.agent.tooling import Tool
from app.agent.trace import collect_usage, separate_trace
from app.capabilities.agent import AgentRunner
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import now_cst, to_cst_full
from app.runtime.lane_policy import current_deployment_lane
from app.world.wake import WORLD

logger = logging.getLogger(__name__)

# Dynamic Config：主 agent 用哪个模型（别的几类没单独配时也用它）、一次调用最多几次带工具
# 的模型调用。改它们不用重新部署。
WORLD_MODEL_KEY = "world_model"
DEFAULT_WORLD_MODEL = "offline-model"
WORLD_RECURSION_LIMIT_KEY = "world_recursion_limit"
# 主 agent 一轮典型要做的事：列目录、读几份、查一次天气或搜一下、写一两份、报告一两个变化、
# 定时刻。一次模型调用可以同时调好几个工具，12 次留了余量；另外三类做的事更少，同一个上限
# 够用。
DEFAULT_WORLD_RECURSION_LIMIT = 12


@dataclass(frozen=True)
class AgentKind:
    """world 里的一类 agent：Langfuse 上的 prompt、trace 名、Dynamic Config 里的模型键。"""

    prompt_id: str
    trace_name: str
    model_key: str


def lane() -> str:
    return current_deployment_lane() or "prod"


def session_key() -> str:
    """world 的 langfuse session，也是主 agent 连续上下文的存储键：按泳道分。"""
    return f"{WORLD}:{lane()}"


def when(moment: datetime) -> str:
    """摆到 agent 眼前的时刻：年月日、星期、时分。"""
    return to_cst_full(moment.isoformat())


async def _model(kind: AgentKind) -> str:
    for key in (kind.model_key, WORLD_MODEL_KEY):
        model = await asyncio.to_thread(dynamic_config.get, key, default="")
        if model.strip():
            return model.strip()
    return DEFAULT_WORLD_MODEL


async def agent_config(kind: AgentKind) -> AgentConfig:
    limit = await asyncio.to_thread(
        dynamic_config.get_int,
        WORLD_RECURSION_LIMIT_KEY,
        default=DEFAULT_WORLD_RECURSION_LIMIT,
    )
    if limit <= 0:
        logger.warning(
            "dynamic config %s = %r is not a positive integer; using %d",
            WORLD_RECURSION_LIMIT_KEY,
            limit,
            DEFAULT_WORLD_RECURSION_LIMIT,
        )
        limit = DEFAULT_WORLD_RECURSION_LIMIT
    return AgentConfig(
        kind.prompt_id,
        await _model(kind),
        kind.trace_name,
        recursion_limit=limit,
    )


def build_runner(config: AgentConfig, tools: list[Tool]) -> AgentRunner:
    """一次调用的 agent。模块级函数，测试替身从这里换掉，不碰真模型。"""
    return AgentRunner(config, tools=tools)


async def run_agent(
    kind: AgentKind,
    messages: list[Turn],
    *,
    tools: list[Tool],
    context: AgentContext,
    call_id: str,
    transcript_sink: list[Turn] | None = None,
) -> Turn:
    """以 ``kind`` 这一类调一次模型，交回它最后说的那条消息。

    ``messages`` 是接在 prompt 后面的全部输入；``transcript_sink`` 给了就收下这次调用产生的
    每一条（主 agent 用它存连续上下文）。模型调用失败原样往外抛。
    """
    started = now_cst()
    runner = build_runner(await agent_config(kind), tools)
    with separate_trace(), collect_usage() as usage:
        reply = await runner.run(
            messages,
            context=context,
            max_retries=1,
            transcript_sink=transcript_sink,
        )
    await record_round_cost(
        lane=lane(),
        actor=WORLD,
        round_id=f"{kind.trace_name}:{call_id}",
        usage=usage,
        observed_at=started.isoformat(),
    )
    return reply
