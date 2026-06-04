"""life 工具循环的工具 (Task 3, agent 工具循环).

某姐妹被 event 唤醒后跑一个 ReAct 循环，连续调这两件工具行动；她的"想法 / 情绪 /
意图"由工具调用产出，不再填一张 LifeDecision 表：

  * :func:`build_life_tools` 造的 ``update_life_state`` —— 更新她此刻在干嘛 /
    什么情绪 / 这算哪类活动。**可调 0 次或多次，多次以最后一次为准**（spec
    决策 2）：每次 ``insert_append`` 一版主观快照，收口对外读 ``select_latest``
    取最新一版，等价"最后一次为准"。

  * ``raise_intent`` —— 她觉得想做点什么就起个意图回灌 world 去裁决。intent_id
    不由模型生成、而是本轮 (lane + persona + 读到的 event_ids) 派生的确定值
    （在节点里算好、capture 进闭包）：整轮重放同一批唤醒时产同一个 intent_id，
    world 端按 intent_id 幂等消化，不会重复起意图。

为什么是 per-round 闭包而不是 module-level @tool：工具要把"她是谁 / 哪个泳道 /
本轮 intent_id / 观测时刻"这些**机制绑定** capture 进去，模型只看见业务参数
（current_state / response_mood / activity_type / summary），看不见 lane /
intent_id 这些不该让它填的东西。AgentContext 是 chat 链路共享的 frozen 契约
（Task 1 owns），不往里塞 life 专用字段；用闭包把绑定收在 life 域里更干净。

失败语义（spec 决策 3）：每件工具叠 ``@tool_error`` —— 单个工具自身抛错时吞掉、
把结构化 outcome 喂回模型让它自纠，不炸整轮。整轮重放的关闭由节点调 ``run``
传 ``max_retries=1`` 负责（见 life_wake）。
"""

from __future__ import annotations

import logging

from app.agent.tooling import Tool
from app.agent.tools._common import tool_error

# module-level 引用 handler，让测试能 monkeypatch（与 mailbox / world_events 同款）。
from app.domain.life_state import save_life_state
from app.domain.world_events import raise_intent

logger = logging.getLogger(__name__)


def build_life_tools(
    *,
    lane: str,
    persona_id: str,
    intent_id: str,
    observed_at: str,
) -> list[Tool]:
    """造这一轮 life 的工具集，把本轮机制绑定 capture 进闭包。

    ``lane`` / ``persona_id`` —— 她是谁、哪个泳道（durable 写的 Key 命门）。
    ``intent_id`` —— 本轮派生的确定意图键（整轮重放幂等），不让模型生成。
    ``observed_at`` —— 本轮观测时刻（ISO8601），快照 / 意图都用它，使重放一致。
    """

    # 本轮是否已起过意图（round-scoped，随这一轮的闭包活着）。intent_id 由本轮
    # event_ids 派生，同一轮 N 次 raise_intent 共用同一个 intent_id —— 第二个及以后
    # 会被 durable 去重层按 intent_id 静默吞掉、意图无声丢失。第一刀：一轮只允许
    # 一个意图生效，第二次调用不再落 handler，而是 log + 喂回提示（绝不静默吞）。
    intent_raised = False

    @tool_error("更新此刻状态失败")
    async def update_life_state(
        current_state: str,
        response_mood: str,
        activity_type: str,
    ) -> str:
        """更新你此刻的主观状态：你现在在做什么、是什么心情、这算哪一类活动。

        想改就调；可以调多次（以最后一次为准），也可以一次都不调（你看了但没改）。

        Args:
            current_state: 你此刻在干嘛（自然语言）。
            response_mood: 此刻的情绪 / 回应基调。
            activity_type: 活动类型（sleep / study / rest / move / idle ...）。

        Returns:
            一句确认。
        """
        await save_life_state(
            lane=lane,
            persona_id=persona_id,
            current_state=current_state,
            response_mood=response_mood,
            activity_type=activity_type,
            observed_at=observed_at,
        )
        return "状态已更新"

    @tool_error("起意图失败")
    async def raise_intent_tool(summary: str) -> str:
        """起一个"我想做点什么"的意图，回灌给世界去裁决。

        多数时候你只是经历这一刻——感知到周遭的动静、心里有点波澜，更新一下自己
        此刻的状态就够了，不需要起意图。只有当你真的要改变自己的处境时才起：比如
        离席回房、出门、特意去找谁、做一件超出此刻正在做的事。仅仅是回应刚才的一点
        动静、心里动了一下，不算改变处境，不用起意图。

        一轮只允许一个意图生效：这一刻只能表达一个想做的事，想清楚再起；没有就不用调。

        Args:
            summary: 这个意图的文字描述。

        Returns:
            一句确认。
        """
        nonlocal intent_raised
        if intent_raised:
            # 本轮已起过一个意图：第二次及以后不再落 handler（共用同一个 intent_id
            # 会被 durable 去重层静默吞掉）。log + 把这句喂回模型让它知道这轮已经
            # 表达过意图了，绝不静默吞。
            logger.warning(
                "[life_tools] %s/%s raise_intent called again in one round "
                "(intent_id=%s); only the first intent takes effect this round, "
                "ignoring summary=%r",
                lane,
                persona_id,
                intent_id,
                summary,
            )
            return "你这一轮已经起过一个意图了，一轮只生效一个；这条没有起，留到下一刻再表达。"
        await raise_intent(
            lane=lane,
            intent_id=intent_id,
            persona_id=persona_id,
            summary=summary,
            occurred_at=observed_at,
        )
        intent_raised = True
        return "意图已起"

    # raise_intent_tool 的函数名带 _tool 后缀避免遮蔽导入的 handler；工具对模型
    # 暴露的 name 要是 "raise_intent"，所以显式覆写 Tool.name 与 definition.name。
    update_tool = Tool(update_life_state)
    intent_tool = Tool(raise_intent_tool)
    intent_tool.name = "raise_intent"
    intent_tool.definition.name = "raise_intent"
    return [update_tool, intent_tool]
