"""life 工具循环的工具 (Task 3 + 阶段 1A act 范式, agent 工具循环).

某姐妹被 event 唤醒后跑一个 ReAct 循环，连续调这两件工具行动；她的"想法 / 情绪 /
做的事"由工具调用产出，不再填一张 LifeDecision 表：

  * :func:`build_life_tools` 造的 ``update_life_state`` —— 更新她此刻在干嘛 /
    什么情绪 / 这算哪类活动。**可调 0 次或多次，多次以最后一次为准**（spec
    决策 2）：每次 ``insert_append`` 一版主观快照，收口对外读 ``select_latest``
    取最新一版，等价"最后一次为准"。

  * ``act`` —— 她**自主做一件影响外部世界的事**（自然语言，如"我去厨房做饭"），
    直接汇给 world 让它推演客观结果。新范式：角色完全自主，act 是"她做了"、不是
    "申请待批准"。act_id 不由模型生成、而是本轮 (lane + persona + 读到的
    event_ids) 派生的确定值（在节点里算好、capture 进闭包）：整轮重放同一批唤醒
    时产同一个 act_id，world 端按 act_id 幂等消化，不会重复推演同一个动作。

为什么是 per-round 闭包而不是 module-level @tool：工具要把"她是谁 / 哪个泳道 /
本轮 act_id / 观测时刻"这些**机制绑定** capture 进去，模型只看见业务参数
（current_state / response_mood / activity_type / description），看不见 lane /
act_id 这些不该让它填的东西。AgentContext 是 chat 链路共享的 frozen 契约
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
from app.domain.world_events import perform_act

logger = logging.getLogger(__name__)


def build_life_tools(
    *,
    lane: str,
    persona_id: str,
    act_id: str,
    observed_at: str,
) -> list[Tool]:
    """造这一轮 life 的工具集，把本轮机制绑定 capture 进闭包。

    ``lane`` / ``persona_id`` —— 她是谁、哪个泳道（durable 写的 Key 命门）。
    ``act_id`` —— 本轮派生的确定动作键（整轮重放幂等），不让模型生成。
    ``observed_at`` —— 本轮观测时刻（ISO8601），快照 / 动作都用它，使重放一致。
    """

    # 本轮是否已做过一件事（round-scoped，随这一轮的闭包活着）。act_id 由本轮
    # event_ids 派生，同一轮 N 次 act 共用同一个 act_id —— 第二个及以后会被 durable
    # 去重层按 act_id 静默吞掉、动作无声丢失。第一刀：一轮只允许做一件事生效，
    # 第二次调用不再落 handler，而是 log + 喂回提示（绝不静默吞）。
    act_performed = False

    @tool_error("更新此刻状态失败")
    async def update_life_state(
        current_state: str,
        response_mood: str,
        activity_type: str,
    ) -> str:
        """更新你此刻的主观状态：你现在在做什么、是什么心情、这算哪一类活动。

        只发生在你自己身上、外面没人会因此察觉到不同的事（你在做什么、什么心情），
        记在这里就够了。想改就调；可以调多次（以最后一次为准），也可以一次都不调。

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

    @tool_error("做这件事失败")
    async def act_tool(description: str) -> str:
        """你做了一件会在你之外的世界留下痕迹的事。

        多数时候你只是经历这一刻——感知到周遭的动静、心里有点波澜，更新一下此刻
        状态（update_life_state）就够了，不用 act。只有当你做的事会在你之外留下
        痕迹、被够得着的人感知到时才 act：端着饭菜出去摆上桌、出门、走到谁面前、
        弄出动静。act 是"你做了"，不是"你请求"——你做了，世界会推演它的客观结果，
        旁边够得着的人迟早会察觉到（世界怎么回应、谁注意到，要等它真在你之外发生，
        你当场未必知道）。只在心里转了一下、顺耳听过刚才的动静、没在外面留下任何
        痕迹，就不算，不用 act。

        一轮只生效一件事：这一刻只能做一件，想清楚再做；没有就不用调。

        Args:
            description: 你做了什么，自然语言描述（如"我去厨房做饭"）。

        Returns:
            一句确认。
        """
        nonlocal act_performed
        if act_performed:
            # 本轮已做过一件事：第二次及以后不再落 handler（共用同一个 act_id 会被
            # durable 去重层静默吞掉）。log + 把这句喂回模型让它知道这轮已经做过一件
            # 事了，绝不静默吞。
            logger.warning(
                "[life_tools] %s/%s act called again in one round "
                "(act_id=%s); only the first act takes effect this round, "
                "ignoring description=%r",
                lane,
                persona_id,
                act_id,
                description,
            )
            return "你这一轮已经做了一件事了，一轮只生效一件；这件没有做，留到下一刻。"
        await perform_act(
            lane=lane,
            act_id=act_id,
            persona_id=persona_id,
            description=description,
            occurred_at=observed_at,
        )
        act_performed = True
        return "已经做了"

    # act_tool 的函数名带 _tool 后缀避免遮蔽导入的 handler；工具对模型暴露的 name
    # 要是 "act"，所以显式覆写 Tool.name 与 definition.name。
    update_tool = Tool(update_life_state)
    act_tool_obj = Tool(act_tool)
    act_tool_obj.name = "act"
    act_tool_obj.definition.name = "act"
    return [update_tool, act_tool_obj]
