"""感知判断 agent：世界里发生了一个变化，谁会察觉、各自察觉到的是什么；判断完由代码告知他们。

**每个变化调用一次**（:func:`tell_who_notices`）。主 agent 报告一个变化
（:func:`app.world.actions.report_change`）就走这里：起一个感知判断 agent，它拿到全部已启用
知识来源的查询工具（:func:`app.world.sources.query_tools`），加上只有它有的
:func:`someone_notices`——每判断一个会察觉的人调一次，写下那个人察觉到的是什么。谁会察觉
完全是它依据各来源做的判断：代码里没有按位置、距离或者任何规则决定感知。

**告知居民只有这一条路。** 它判断完，代码把每一条判断原样按通信机制发给那个参与者（``send``，
发送方是 world），把告知了谁、送没送达交回给调用方。主 agent 没有直接给谁发消息的工具，所以
"谁知道这件事"只由这一次判断决定，告知的内容也是判断写下的那段话。

**没送达不另外处理。** 对方没开设收件箱时 ``send`` 不投递、记下来、当场交回"没有送达"，不会
再给 world 发一条告知，也就不会叫醒它。发送本身出错（记录写不进去、broker 没确认）只影响那
一条，交回"没有发出"，别的照发。名字就是参与者在这个世界里的名字，通信机制校验不过的，在
判断那一刻就退回给感知判断 agent，让它改。

prompt 在 Langfuse（:data:`PERCEPTION`），正文不引用任何变量；现在几点、这一次的变化写在
USER 消息里。
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Annotated

from pydantic import Field

from app.agent.context import AgentContext
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities._errors import CapabilityInvalidArg
from app.infra.cst_time import now_cst
from app.messaging.message import SendFailed, participant
from app.messaging.sending import send
from app.world.agents import AgentKind, run_agent, session_key, when
from app.world.sources import query_tools
from app.world.wake import WORLD

logger = logging.getLogger(__name__)

PERCEPTION = AgentKind(
    prompt_id="world_perception",
    trace_name="world-perception",
    model_key="world_perception_model",
)

# ``AgentContext.features`` 里这一次判断的结果：参与者名字 → 他察觉到的那段话。
_JUDGMENTS = "world_perception_judgments"


@tool
@tool_error("没有记下这条判断")
async def someone_notices(
    who: Annotated[
        str, Field(description="会察觉到的那个参与者，用他在这个世界里的名字")
    ],
    what: Annotated[
        str,
        Field(
            description=(
                "他察觉到的是什么：从他的角度写，什么时候、在哪、他看到听到感觉到了什么。"
                "这段话会原样发给他"
            )
        ),
    ],
) -> str:
    """判断一个参与者会察觉到这个变化，写下他察觉到的是什么。

    每个会察觉的人调一次；同一个人再调一次，以最后一次为准。没有人会察觉，就一次都不调。
    """
    name = who.strip()
    try:
        participant(name)
    except ValueError as exc:
        raise CapabilityInvalidArg(f"「{who}」不是通信机制收得下的参与者名字：{exc}") from exc
    if name == WORLD:
        raise CapabilityInvalidArg(f"「{WORLD}」是世界自己，不用告知")
    if not what.strip():
        raise CapabilityInvalidArg("写下他察觉到的是什么：这段话会原样发给他")
    get_context().features[_JUDGMENTS][name] = what.strip()
    return f"记下了：{name} 会察觉到。"


@dataclass(frozen=True)
class Notice:
    """一条告知：发给谁、发的什么、送没送达（没送达时为什么）。"""

    who: str
    what: str
    delivered: bool
    reason: str | None = None


def _perception_input(change: str) -> str:
    return "\n".join([f"【现在】{when(now_cst())}", "【世界里发生的变化】", change])


async def _tell(who: str, what: str) -> Notice:
    try:
        delivery = await send(sender=WORLD, recipient=who, body=what)
    except SendFailed as exc:
        logger.error("world: notice to %s was not sent: %s", who, exc)
        return Notice(who, what, delivered=False, reason=f"通信机制出错，这一条没有发出：{exc}")
    return Notice(who, what, delivery.delivered, delivery.reason)


async def tell_who_notices(change: str) -> list[Notice]:
    """为一个变化跑一次感知判断，把每一条判断告知那个人，交回告知的结果。

    感知判断那次模型调用失败原样往外抛，这时一条都还没发。
    """
    judgments: dict[str, str] = {}
    call_id = uuid.uuid4().hex
    await run_agent(
        PERCEPTION,
        [Turn(role=Role.USER, content=_perception_input(change))],
        tools=[*await query_tools(), someone_notices],
        context=AgentContext(session_id=session_key(), features={_JUDGMENTS: judgments}),
        call_id=call_id,
    )
    notices = [await _tell(who, what) for who, what in judgments.items()]
    logger.info(
        "world: perception %s told %s",
        call_id,
        ", ".join(f"{n.who}({'delivered' if n.delivered else n.reason})" for n in notices)
        or "nobody",
    )
    return notices


def render_notices(notices: list[Notice]) -> str:
    """交给主 agent 看的告知结果：告知了谁、告知的什么、送没送达。"""
    if not notices:
        return "感知判断：没有人会察觉到这个变化，没有告知任何人。"
    lines = ["感知判断之后，告知了这些人："]
    for n in notices:
        status = "送达了" if n.delivered else f"没有送达（{n.reason}）"
        lines.append(f"- {n.who}：「{n.what}」——{status}")
    return "\n".join(lines)
