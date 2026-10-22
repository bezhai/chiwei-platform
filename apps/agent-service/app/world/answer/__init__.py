"""插件 ``answer``：应答 agent，回答别人问 world 的问题——某处现在什么样、谁在哪。

world 的收件箱声明了 ``on_question``（:mod:`app.world.core`），把提问交给在 ``world_intake`` 上
登记的应答；这个插件登记的就是它。每个问题起一个应答 agent（:meth:`Answering.answer_question`）：
它拿到清单给它列的工具（宿主组好的 ``world_answer``，``ctx.agent``）：知识来源的查询工具，跟另外
三类拿的是同一份；它不在哪一轮里，关掉的来源按每个问题来时读的开关拿掉，prompt 里关掉的插件的段落
也一样。它依据这些工具回答。能当场推出来的东西（按现在的时间、天气和记录）由它当场推，主 agent
不为此提前写下，也不为此醒。

**只读。** 它不写记录、不发消息、不报告变化，也不叫醒主 agent：它手里只有来源的查询工具。
它不在主 agent 的任何一轮里，可以和一轮同时跑。提问也不经过各来源的收件处理——那只在普通消息那条路上
跑（:meth:`app.world.core.rounds.Rounds.receive`）。问的人拿到的就是它最后说的话；它什么都没
说，就是"没有回答"。

prompt 在 Langfuse（:data:`ANSWER`），正文不引用任何变量；现在几点、谁问的、问的什么写在 USER
消息里。
"""
from __future__ import annotations

import logging
import uuid

from app.agent.contracts import Switches
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.agent.prompts import PromptSet
from app.agent.round import AssembledAgent, open_call, switched_off
from app.host import Context, Plugin
from app.infra.cst_time import now_cst
from app.messaging.message import Message
from app.world.agents import WORLD, lane, run_agent, when, world_agent, world_segments
from app.world.services import WorldIntake

logger = logging.getLogger(__name__)

ANSWER = "world_answer"
ANSWER_AGENT = world_agent(ANSWER, "world-answer", "world_answer_model")


def _answer_input(question: Message) -> str:
    return "\n".join(
        [
            f"【现在】{when(now_cst())}",
            f"【{question.sender} 问】（{when(question.time)}）",
            question.body,
        ]
    )


class Answering:
    """回答问 world 的问题。``agent`` 是宿主组好的应答 agent（``ctx.agent``），``switches`` 是 world
    一轮的开关（清单 ``world`` 的 ``switches``）：应答跟主 agent 关掉同样的来源。"""

    def __init__(self, agent: AssembledAgent, switches: Switches | None) -> None:
        self._agent = agent
        self._switches = switches

    async def answer_question(self, question: Message) -> str | None:
        """world 收件箱的 ``on_question``：起一个应答 agent 回答它；什么都没说就是 ``None``。

        模型调用失败原样往外抛，通信机制告诉提问方"没有回答"，不重试。
        """
        call_id = uuid.uuid4().hex
        spec = self._agent.spec
        # 应答不在主 agent 的哪一轮里：开关在这个问题来时读，prompt 在这次调用开始时取。
        off = await switched_off(self._switches)
        tools = self._agent.tools_on(off)
        prompts = await PromptSet.resolve(
            {ANSWER: spec},
            self._agent.segments,
            None,
            participant=WORLD,
            switched_off=off,
        )
        reply = await run_agent(
            open_call(ANSWER, spec, lane=lane()),
            spec,
            [Turn(role=Role.USER, content=_answer_input(question))],
            prompts=prompts,
            tools=tools,
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


def setup(ctx: Context) -> None:
    intake: WorldIntake = ctx.service("world_intake")
    answering = Answering(ctx.agent(ANSWER), ctx.profile("world").profile.switches)
    ctx.on_stop(intake.answer_with("answer", answering.answer_question))
    for segment in world_segments(ANSWER).values():
        ctx.segment(segment)


PLUGIN = Plugin(name="answer", setup=setup, requires=("world_intake",))
