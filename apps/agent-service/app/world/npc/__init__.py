"""插件 ``npc``：主 agent 让某个 NPC 在一次互动里出场，一个临时 agent 扮演他，给出他说的话、做的事。
它登记让 NPC 出场的动作（:meth:`Npcs.let_npc_appear`）、NPC agent 的 prompt 和主 agent 的 prompt 里
讲 NPC 的那一段（``world_round/npc``），用 ``perception`` 服务判断谁会察觉、记下并告知。

**NPC 的言行不经主 agent 转述。** 主 agent 只说让谁出场、在什么情境下
（:meth:`Npcs.let_npc_appear`）；扮演他的 agent 依据各知识来源（他是谁、他在的
地方、刚刚发生了什么）给出他这一次说的话、做的事。这段输出原样作为一个变化交给感知判断
（``perception`` 的 ``judge_who_notices``），谁会察觉、察觉到什么由那边判断，判断完由那边记下、
告知（``tell``）；主 agent 看到他的言行和告知了谁，事后把这次互动留下的东西记进记录。主 agent
一转述，NPC 的话就成了 world 代写的。

**一次互动一个临时 agent。** 它没有自己的连续上下文，扮演完就结束；拿到清单给它列的工具（宿主
组好的 ``world_npc``，``ctx.agent``）：知识来源的查询工具，关掉的来源按这一轮开始时读的开关拿掉，
别的什么都不能做：不写记录，也不发消息。

prompt 在 Langfuse（:data:`NPC`），正文不引用任何变量；现在几点、扮演谁、什么情境写在 USER
消息里。
"""
from __future__ import annotations

import logging
import uuid
from typing import Annotated

from pydantic import Field

from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.round import AssembledAgent, current_round, open_call
from app.agent.tooling import tool
from app.host import Context, Plugin
from app.infra.cst_time import now_cst
from app.messaging.message import message_body
from app.world.agents import (
    lane,
    not_done,
    run_agent,
    when,
    world_agent,
    world_segments,
)
from app.world.services import Perception

logger = logging.getLogger(__name__)

NPC = "world_npc"
NPC_AGENT = world_agent(NPC, "world-npc", "world_npc_model")

# 主 agent 的 prompt 里讲 NPC 的那一段（Langfuse id）。
NPC_SEGMENT = "world_round/npc"


def _npc_input(npc: str, situation: str) -> str:
    return "\n".join(
        [f"【现在】{when(now_cst())}", "【你扮演的人】", npc, "【这次出场的情境】", situation]
    )


class Npcs:
    """让 NPC 出场：主 agent 的动作 :meth:`let_npc_appear`。``agent`` 是宿主组好的 NPC agent
    （``ctx.agent``），``perception`` 是判断谁会察觉、记下并告知的那一方。"""

    def __init__(self, agent: AssembledAgent, perception: Perception) -> None:
        self._agent = agent
        self._perception = perception

    async def play(self, npc: str, situation: str) -> str:
        """起一个临时 agent 扮演 ``npc``，交回他这一次的言行（原样，去掉首尾空白；可能是空的）。

        模型调用失败原样往外抛。
        """
        call_id = uuid.uuid4().hex
        # 扮演是在主 agent 这一轮里做的，prompt 用这一轮开头定下的那个版本，工具按这一轮开始时
        # 读的开关。
        world_round = current_round()
        spec = self._agent.spec
        call = open_call(NPC, spec, lane=lane(), round=world_round)
        reply = await run_agent(
            call,
            spec,
            [Turn(role=Role.USER, content=_npc_input(npc, situation))],
            prompts=world_round.prompts,
            tools=self._agent.tools_on(world_round.switched_off),
            call_id=call_id,
        )
        acted = reply.text().strip()
        logger.info("world: npc call %s played %s: %s", call_id, npc[:40], acted[:200])
        return acted

    # 主 agent 的动作，不包 @tool_error：判断完交给 ``perception`` 记下、告知之后的失败不收住
    # （见 :mod:`app.world.perception`）。

    async def let_npc_appear(
        self,
        npc: Annotated[str, Field(description="出场的是谁：他在记录里的名字和身份")],
        situation: Annotated[
            str,
            Field(
                description=(
                    "这次出场的情境：什么时候、在哪、刚刚发生了什么、谁在跟他打交道。只写情境，"
                    "不写他要说什么、做什么"
                )
            ),
        ],
    ) -> str:
        """让一个 NPC 在一次互动里出场。

        一个临时 agent 依据各来源扮演他，给出他这一次说的话、做的事；这段言行原样交给感知判断，
        告知会察觉到的人。返回他的言行和告知了谁。这次互动留下了什么，之后由你记进记录。
        """
        try:
            name, situation = message_body(npc.strip()), message_body(situation.strip())
        except ValueError as exc:
            return f"没有出场：写下出场的是谁、这次出场的情境，里面不能有记不下来的东西（{exc}）。"
        try:
            acted = await self.play(name, situation)
            if not acted:
                return f"{name} 这一次没有说话，也没有做什么。没有告知任何人。"
            notices = await self._perception.judge_who_notices(
                message_body(acted), round_messages=current_round().due.messages
            )
        except Exception as exc:
            return not_done(f"{name} 没有出场，扮演或者感知判断没有做成", exc)
        words = f"【{name} 这一次的言行】\n{acted}"
        told = await self._perception.tell(
            f"你让 {name} 出场，情境：{situation}\n{words}", notices
        )
        return f"{words}\n\n{told}"


def setup(ctx: Context) -> None:
    perception: Perception = ctx.service("perception")
    npcs = Npcs(ctx.agent(NPC), perception)
    for segment in world_segments(NPC, NPC_SEGMENT).values():
        ctx.segment(segment)
    ctx.tool(tool(npcs.let_npc_appear), trim="kept")


PLUGIN = Plugin(name="npc", setup=setup, requires=("perception",))
