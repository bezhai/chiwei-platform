"""应答 agent：回答别人问 world 的问题——某处现在什么样、谁在哪。

world 的收件箱声明了 ``on_question``（:mod:`app.plugins.world`），每个问题起一个应答 agent：它拿到
全部已启用知识来源的查询工具（:func:`app.world.sources.query_tools`，跟另外三类拿的是同一份），
依据它们回答。能当场推出来的东西（按现在的时间、天气和记录）由它当场推，主 agent 不为此提前
写下，也不为此醒。

**只读。** 它不写记录、不发消息、不报告变化，也不叫醒主 agent：它手里只有来源的查询工具，
这一次调用也不放记读过什么的那个字典。提问也不经过各来源的收件处理——那只在普通消息那条路上
跑（:meth:`app.world.rounds.Rounds.receive`）。问的人拿到的就是它最后说的话；它什么都没
说，就是"没有回答"。

prompt 在 Langfuse（:data:`ANSWER`），正文不引用任何变量；现在几点、谁问的、问的什么写在 USER
消息里。
"""
from __future__ import annotations

import logging
import uuid

from app.agent.context import AgentContext
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.infra.cst_time import now_cst
from app.messaging.message import Message
from app.world.agents import AgentKind, run_agent, session_key, when
from app.world.sources import query_tools

logger = logging.getLogger(__name__)

ANSWER = AgentKind(
    prompt_id="world_answer", trace_name="world-answer", model_key="world_answer_model"
)


def _answer_input(question: Message) -> str:
    return "\n".join(
        [
            f"【现在】{when(now_cst())}",
            f"【{question.sender} 问】（{when(question.time)}）",
            question.body,
        ]
    )


async def answer_question(question: Message) -> str | None:
    """world 收件箱的 ``on_question``：起一个应答 agent 回答它；什么都没说就是 ``None``。

    模型调用失败原样往外抛，通信机制告诉提问方"没有回答"，不重试。
    """
    call_id = uuid.uuid4().hex
    reply = await run_agent(
        ANSWER,
        [Turn(role=Role.USER, content=_answer_input(question))],
        tools=await query_tools(),
        context=AgentContext(session_id=session_key()),
        call_id=call_id,
    )
    text = reply.text().strip()
    logger.info(
        "world: answer call %s for question %s from %s: %s",
        call_id,
        question.message_id,
        question.sender,
        text[:200] or "(no answer)",
    )
    return text or None
