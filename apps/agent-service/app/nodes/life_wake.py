"""life_wake_node — 三姐妹同构的 life 节点 (Task 3, agent 工具循环).

一套逻辑跑 akao / chinagi / ayana 三个 persona——不是三份拷贝。她是谁由唤醒她的
``EventArrived.persona_id`` 决定。

被 ``EventArrived`` 攒批唤醒后，她跑一个 **ReAct 工具循环**（不再填一张表）：

  1. 读自己的 ``LifeState``（主观快照）+ 读自己信箱里的未读 event。
  2. 跑 ``Agent(...).run`` —— 在循环里连续调工具行动：``update_life_state``
     更新此刻状态（0/N 次，多次以最后一次为准），``raise_intent`` 起意图回灌
     world。她想啥、做啥、什么情绪、要不要起意图，全由模型在循环里自己定。
  3. 收口：标已读 —— **只标本轮实际读到的那批 event_id**（即使一次 update 都没
     调也照常标已读：她看了但没改状态，正常）。

钉死的几条（spec / 宪法）：

  * **信息差命门**：一轮的输入 = 她自己的 ``LifeState`` + 她自己信箱的未读 event。
    本模块**绝不 import / 读 WorldState 全局快照**——全局真相一旦漏进她上下文，
    她就全知了，信息差崩塌。结构上保证：这里没有任何读 world 快照的代码。

  * **空信箱 early-return**：信箱没未读就不烧模型、不建工具、不写、不标已读。

  * **single_flight 锁**：一轮思考几十秒 > debounce 窗口，期间来新 event 会 fire
    第二轮并发；开头按 ``(lane, persona)`` 拿单飞锁，拿不到就 raise
    ``DebounceReschedule`` 交给 handler 重排（这批 event 不被吞）。

  * **失败不整轮重放**：``run`` 把整个 ReAct 循环包在 retry 里，一次 model 调用
    瞬时失败会整轮重放、重放已执行的 durable 工具（重复写快照 / 重复起意图）。
    所以 life 调 ``run`` 传 ``max_retries=1`` 关掉整轮重放；中途失败就抛、本轮
    不收口（event 没标已读 → 下轮仍未读、靠 world renotify 再唤醒）。

  * **无 state_end_at、不自排闹钟**：她脑子里没有"做到几点"，只有"此刻什么样"。
    她被 event 推、**不 emit_delayed / emit_at 给自己定时唤醒**。

  * **赤尾设计宪法**：她想啥、做啥、什么情绪、要不要起意图，全由模型在循环里
    判断。本模块不用阈值 / 计数器 / 随机池 / if 分支替她决策——只做 IO 编排 +
    机制安全阀（单飞锁、空信箱、inbox 上限）。

intent_id 从 ``(lane, persona, 本轮读到的 event_ids)`` 派生（durable 边重投 / 重试
同一批唤醒产同一个 intent_id，world 按 intent_id 幂等消化），在本节点算好后
capture 进 ``build_life_tools`` 的闭包，不让模型生成。

wiring 见 ``app/wiring/life_dataflow.py``，本模块只提供 ``@node`` 函数 + 依赖。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from app.agent.context import AgentContext
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.agent.trace import make_session_id
from app.data.queries.mailbox import list_unread_events, mark_events_read
from app.domain.life_state import find_life_state
from app.domain.world_events import EventArrived, EventEnvelope
from app.memory._persona import load_persona
from app.nodes.life_tools import build_life_tools
from app.runtime import node
from app.runtime.debounce import DebounceReschedule
from app.runtime.single_flight import SingleFlightConflict, single_flight

logger = logging.getLogger(__name__)

# life 单飞锁的 TTL：比一轮 life 思考的最坏耗时更大的上界（LLM 几十秒级 + 工具
# 循环多轮）。锁只是基建并发控制（不替 agent 决策、不违反赤尾宪法），TTL 到期后
# 哪怕原 holder 还在跑、新 holder 也能进，token-CAS 释放保证不误删别人的锁。
_LIFE_WAKE_LOCK_TTL_SECONDS = 600

# 一轮读 inbox 的上限（spec 决策 4 安全阀）：正常够不着；积压过多时只读这批喂给
# 模型 + 只标这批已读，剩下的留未读、下轮再处理（不静默吞）。触顶要 log。
_LIFE_INBOX_MAX = 50

# offline-model：异步后台思考用离线模型（见 feedback_model_selection），主对话才用
# gemini。recursion_limit 给够（让她在一轮里连续调多次工具，不被默认 6 卡住）。
# trace_name 让这一轮 life 思考接进 langfuse。
_LIFE_WAKE_CFG = AgentConfig(
    "life_wake", "offline-model", "life-wake", recursion_limit=12
)

# observed_at 用 ISO8601 UTC；展示层时区由读取方处理。
_TZ = UTC


def _format_unread(unread: list[EventEnvelope]) -> str:
    """把未读 event 拼成她"刚感知到 / 想起的几件事"的文字，按发生时间顺序。

    只放 event 的客观可感形态（summary）+ 类型 + 发生时间——这些都是投进她信箱的、
    她够得着的信息，不含任何 world 全局视角。
    """
    return "\n".join(
        f"- [{ev.kind}] {ev.occurred_at} {ev.summary}" for ev in unread
    )


@node
async def life_wake_node(arrived: EventArrived) -> None:
    """某姐妹被攒批唤醒，跑一轮 life 工具循环。persona 由 ``arrived`` 决定。

    **单飞命门**：一轮 life 跑几十秒 > debounce 窗口（5s），期间来新 event 会 fire
    第二轮 ``life_wake_node`` 并发。两轮并发会互相覆盖 LifeState、把 event 静默标
    已读丢掉。所以开头按 ``(lane, persona)`` 拿一把单飞锁；拿不到锁就 ``raise
    DebounceReschedule``，交给 debounce handler CAS 重排、稍后再试（这一批 event
    不被吞掉）。锁是基建并发控制、不替 agent 决策，不违反赤尾宪法。
    """
    lane = arrived.lane
    persona_id = arrived.persona_id

    lock_key = f"life_wake:{lane}:{persona_id}"
    try:
        async with single_flight(lock_key, ttl=_LIFE_WAKE_LOCK_TTL_SECONDS):
            await _run_life_round(arrived, lane=lane, persona_id=persona_id)
    except SingleFlightConflict:
        # 同 (lane,persona) 已有一轮在跑：不并发跑、不写快照、不标已读。交回
        # debounce handler 重排这一批 EventArrived，等当前那轮跑完后再醒一次。
        logger.info(
            "[life_wake] %s/%s another round in flight, reschedule", lane, persona_id
        )
        raise DebounceReschedule(arrived) from None


async def _run_life_round(arrived: EventArrived, *, lane: str, persona_id: str) -> None:
    """一轮 life 的实际编排（已在单飞锁内）：读未读 → 跑工具循环 → 收口标已读。"""
    unread = await list_unread_events(lane=lane, persona_id=persona_id)
    if not unread:
        # 空唤醒（去重命中后的残留信号等）：不烧模型、不建工具、不写、不标已读。
        logger.info("[life_wake] %s/%s woke with empty inbox, skip", lane, persona_id)
        return

    # 安全阀：一轮 inbox 上限。积压超限只读这批 + 只标这批已读，剩下留未读、下轮
    # 再处理（不静默吞）。触顶要 log（不静默截断）。
    if len(unread) > _LIFE_INBOX_MAX:
        logger.warning(
            "[life_wake] %s/%s inbox backlog %d > cap %d, processing first %d this "
            "round; the rest stay unread for next round",
            lane, persona_id, len(unread), _LIFE_INBOX_MAX, _LIFE_INBOX_MAX,
        )
        unread = unread[:_LIFE_INBOX_MAX]

    snapshot = await find_life_state(lane=lane, persona_id=persona_id)
    pc = await load_persona(persona_id)

    now = datetime.now(_TZ)
    observed_at = now.isoformat()
    prev_state = snapshot.current_state if snapshot else "（还没有此刻状态）"
    prev_mood = snapshot.response_mood if snapshot else ""
    prev_activity = snapshot.activity_type if snapshot else ""

    prompt_vars = {
        "persona_name": pc.display_name,
        "persona_lite": pc.persona_lite,
        "current_time": now.strftime("%H:%M"),
        # 她此刻自己的主观快照（不是 world 全局真相）
        "prev_state": prev_state,
        "prev_mood": prev_mood,
        "prev_activity": prev_activity,
        # 她信箱里这一轮感知到的几件事（客观可感形态）
        "unread_events": _format_unread(unread),
    }

    # intent_id 从 (lane, persona, 本轮读到的 event_ids) 派生 —— durable 边重投 /
    # 重试同一批唤醒时产同一个 intent_id，world 按 intent_id 幂等消化。capture
    # 进工具闭包，不让模型生成。
    read_ids = [ev.event_id for ev in unread]
    seed = f"{lane}:{persona_id}:" + ",".join(sorted(read_ids))
    intent_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

    tools = build_life_tools(
        lane=lane,
        persona_id=persona_id,
        intent_id=intent_id,
        observed_at=observed_at,
    )

    # session 按 (lane, persona, 今天) 派生：她当天所有唤醒的 LLM 调用归进同一条
    # langfuse session，连续看一个角色的"意识流"。
    session_id = make_session_id(lane, persona_id, now.strftime("%Y-%m-%d"))
    context = AgentContext(persona_id=persona_id, session_id=session_id)

    # max_retries=1：关掉整轮重放。run 把整个 ReAct 循环包在 retry 里，一次 model
    # 调用瞬时失败会整轮重放、重放已执行的 durable 工具（重复写快照 / 重复起意图）。
    # 关掉后中途失败就抛、本轮不收口（event 没标已读 → 下轮仍未读、靠 world
    # renotify 再唤醒）。
    await Agent(_LIFE_WAKE_CFG, tools=tools).run(
        messages=[Message(role=Role.USER, content="此刻你感知到了这些，过你自己的这一刻。")],
        prompt_vars=prompt_vars,
        context=context,
        max_retries=1,
    )

    # 收口：标已读，只标本轮实际读到的那批 event_id（绝不按 persona 全标）。即使
    # 一次 update 都没调也照常标已读——她看了但没改状态，正常。
    await mark_events_read(lane=lane, persona_id=persona_id, event_ids=read_ids)
    logger.info(
        "[life_wake] %s/%s ran a round, marked %d read", lane, persona_id, len(read_ids)
    )
