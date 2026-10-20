"""world 的 agent 怎么调模型：主 agent 和三类按需调用的 agent（感知判断、NPC、应答）共用这一处。

每一类是一个 :class:`~app.agent.contracts.AgentSpec`（:func:`world_agent` 造），名字是它的 Langfuse
prompt id。主 agent 的一轮由固定循环跑（:func:`app.agent.round.run_round`，按清单 ``world``），
另外三类经 :func:`run_agent` 调一次模型；工具循环都跑到它不再调工具为止：

* **用哪个模型**走 Dynamic Config：每一类一个键（``model_key``）；没配就跟主 agent 用同一个
  （:data:`WORLD_MODEL_KEY`），那个也没配就是 :data:`DEFAULT_WORLD_MODEL`。所以一开始四类用
  同一个模型，哪一类需要换（应答要快，扮演 NPC 要会写）再单独配，不用改代码。
* **一次调用最多几次带工具的模型调用**四类共用 :data:`WORLD_RECURSION_LIMIT_KEY`。它只是让一次
  调用停得下来的上限，不决定 agent 做什么；撞上它时框架再给一次不带工具的调用收尾。
* **每一次调用是一条单独的 trace**（``separate_trace``），trace 名就是这一类的 ``trace_name``。
  感知判断和 NPC 是在主 agent 的工具里调的，不另起一条就会挂进主 agent 那条 trace，还会把它的
  名字改掉。四类都归到同一个 langfuse session（``world:<泳道>``），按时间顺着看。
* **模型的临时失败不整次重来**（``max_retries`` 1）：感知判断自己重试，见
  :mod:`app.world.perception`。
* **成本**每一次调用记一行（:func:`app.domain.thinking_cost.record_round_cost`）：actor 是
  ``world``，round_id 是 ``<trace 名>:<调用 id>``，按前缀就分得出四类各花了多少。主 agent 的那一行
  由固定循环按清单的成本名记，调用 id 就是这一轮的 id。

工具按声明交给它（:class:`~app.agent.contracts.ToolSpec`）：知识来源的查询工具是读到的材料，
主 agent 的动作和感知判断记判断的那只手留着。
"""
from __future__ import annotations

import logging
from datetime import datetime

from app.agent.contracts import AgentSpec, FromConfig, Segment, ToolSpec
from app.agent.core import Agent
from app.agent.neutral import Message as Turn
from app.agent.prompts import PromptSet
from app.agent.round import Call
from app.agent.tools.outcome import BAD_ARGUMENTS
from app.agent.trace import collect_usage
from app.domain.thinking_cost import record_round_cost
from app.infra.cst_time import now_cst, to_cst_full
from app.infra.lane_policy import current_deployment_lane
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


def lane() -> str:
    return current_deployment_lane() or "prod"


# world 主 agent 连续上下文的存储键（格式串，按泳道分，清单 ``world`` 的历史用它）；填上泳道之后
# 和四类 agent 的 langfuse session 是同一个字符串。
HISTORY_KEY = f"{WORLD}:{{lane}}"


def when(moment: datetime) -> str:
    """摆到 agent 眼前的时刻：年月日、星期、时分。"""
    return to_cst_full(moment.isoformat())


# 四类共用的工具预算。
WORLD_RECURSION_LIMIT = FromConfig(
    keys=(WORLD_RECURSION_LIMIT_KEY,), default=DEFAULT_WORLD_RECURSION_LIMIT
)


def world_agent(
    name: str,
    trace_name: str,
    model_key: str | None = None,
    *,
    segments: tuple[str, ...] | None = None,
) -> AgentSpec:
    """world 的一类 agent：prompt 是 ``segments`` 这几段，没给就是整份一段、id 是它的名字；模型先看
    它自己的键、再看主 agent 的。"""
    keys = (WORLD_MODEL_KEY,) if model_key is None else (model_key, WORLD_MODEL_KEY)
    return AgentSpec(
        segments=(name,) if segments is None else segments,
        model=FromConfig(keys=keys, default=DEFAULT_WORLD_MODEL),
        trace_name=trace_name,
        recursion_limit=WORLD_RECURSION_LIMIT,
        session=f"{WORLD}:{{lane}}",
        separate_trace=True,
        bad_arguments=BAD_ARGUMENTS,
    )


def world_segments(*names: str) -> dict[str, Segment]:
    """world 的这几段 prompt：id 就是段名，正文不引用任何变量。"""
    return {name: Segment(name, name) for name in names}


async def run_agent(
    call: Call,
    spec: AgentSpec,
    messages: list[Turn],
    *,
    prompts: PromptSet,
    tools: list[ToolSpec],
    call_id: str,
    transcript_sink: list[Turn] | None = None,
) -> Turn:
    """以 ``spec`` 这一类、调用方开好的 ``call`` 调一次模型，交回它最后说的那条消息。

    ``prompts`` 是调用方取齐的（一轮里的调用用这一轮的，应答用它开始时取的）；``messages`` 是
    接在 prompt 后面的全部输入；``transcript_sink`` 给了就收下这次调用产生的每一条（主 agent
    用它存连续上下文）。模型调用失败原样往外抛。
    """
    started = now_cst()
    with collect_usage() as usage:
        reply = await Agent(spec, tools=tools).run(
            messages, call=call, prompts=prompts, transcript_sink=transcript_sink
        )
    await record_round_cost(
        lane=lane(),
        actor=WORLD,
        round_id=f"{spec.trace_name}:{call_id}",
        usage=usage,
        observed_at=started.isoformat(),
    )
    return reply
