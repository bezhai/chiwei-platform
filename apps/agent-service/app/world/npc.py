"""NPC agent：主 agent 让某个 NPC 在一次互动里出场，一个临时 agent 扮演他，给出他说的话、做的事。

**NPC 的言行不经主 agent 转述。** 主 agent 只说让谁出场、在什么情境下
（:func:`app.world.actions.let_npc_appear`）；扮演他的 agent 依据各知识来源（他是谁、他在的
地方、刚刚发生了什么）给出他这一次说的话、做的事。这段输出原样作为一个变化交给感知判断
（:func:`app.world.perception.tell_who_notices`），谁会察觉、察觉到什么由那边判断；主 agent
看到他的言行和告知了谁，事后把这次互动留下的东西记进记录。主 agent 一转述，NPC 的话就成了
world 代写的。

**一次互动一个临时 agent。** 它没有自己的连续上下文，扮演完就结束；拿到全部已启用知识来源的
查询工具（:func:`app.world.sources.query_tools`），别的什么都不能做：不写记录，也不发消息。

prompt 在 Langfuse（:data:`NPC`），正文不引用任何变量；现在几点、扮演谁、什么情境写在 USER
消息里。
"""
from __future__ import annotations

import logging
import uuid

from app.agent.context import AgentContext
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.infra.cst_time import now_cst
from app.world.agents import AgentKind, run_agent, session_key, when
from app.world.sources import query_tools

logger = logging.getLogger(__name__)

NPC = AgentKind(prompt_id="world_npc", trace_name="world-npc", model_key="world_npc_model")


def _npc_input(npc: str, situation: str) -> str:
    return "\n".join(
        [f"【现在】{when(now_cst())}", "【你扮演的人】", npc, "【这次出场的情境】", situation]
    )


async def play_npc(npc: str, situation: str) -> str:
    """起一个临时 agent 扮演 ``npc``，交回他这一次的言行（原样，去掉首尾空白；可能是空的）。

    模型调用失败原样往外抛。
    """
    call_id = uuid.uuid4().hex
    reply = await run_agent(
        NPC,
        [Turn(role=Role.USER, content=_npc_input(npc, situation))],
        tools=await query_tools(),
        context=AgentContext(session_id=session_key()),
        call_id=call_id,
    )
    acted = reply.text().strip()
    logger.info("world: npc call %s played %s: %s", call_id, npc[:40], acted[:200])
    return acted
