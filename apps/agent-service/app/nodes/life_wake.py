"""life_wake_node — 三姐妹同构的 life 节点 (Task 3, life engine 三姐妹).

一套逻辑跑 akao / chinagi / ayana 三个 persona——不是三份拷贝。她是谁由唤醒她的
``EventArrived.persona_id`` 决定。

被 ``EventArrived`` 攒批唤醒后，她做一轮：

  1. 读自己的 ``LifeState``（主观快照）+ 读自己信箱里的未读 event。
  2. LLM 想一轮，做主观解读：此刻在干嘛、什么情绪、活动类型，要不要起个意图。
  3. append 新的 ``LifeState``。
  4. 起了意图就 ``raise_intent`` 回灌唤醒 world。
  5. 标已读 —— **只标本轮实际读到的那批 event_id**。

钉死的几条（spec / 宪法）：

  * **信息差命门**：一轮的输入 = 她自己的 ``LifeState`` + 她自己信箱的未读 event。
    本模块**绝不 import / 读 WorldState 全局快照**——全局真相一旦漏进她上下文，
    她就全知了，信息差崩塌。结构上保证：这里没有任何读 world 快照的代码。

  * **无 state_end_at、不自排闹钟**：她脑子里没有"做到几点"，只有"此刻什么样"。
    她被 event 推、**不 emit_delayed / emit_at 给自己定时唤醒**。有客观边界的事
    （上课）结束由 world 到点推 event；没边界的（看书）靠下个 event 打断。

  * **标已读只标本轮**：传给 ``mark_events_read`` 的就是本轮实际 ``list_unread_events``
    读到的那批 event_id。想一轮那几十秒里新进的 event 没在这批里、下一轮仍未读，
    不被静默吞掉（复用 Task 1 的正确性）。

  * **赤尾设计宪法**：她想啥、做啥、什么情绪、要不要起意图，全由 LLM 判断。本模块
    不用阈值 / 计数器 / 随机池 / if 分支替她决策——只做 IO 编排。

LLM 用结构化输出（``Agent.extract``）拿回 :class:`LifeDecision`，不从自然语言抠
语义（见 feedback_llm_output_structured）。

wiring（接 ``wire(EventArrived).debounce(...).to(life_wake_node)``）是 Task 4 的活，
本模块只提供 ``@node`` 函数 + 它的依赖。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role
from app.data.queries.mailbox import list_unread_events, mark_events_read
from app.domain.life_state import find_life_state, save_life_state
from app.domain.world_events import (
    EventArrived,
    EventEnvelope,
    raise_intent,
)
from app.memory._persona import load_persona
from app.runtime import node
from app.runtime.debounce import DebounceReschedule
from app.runtime.single_flight import SingleFlightConflict, single_flight

logger = logging.getLogger(__name__)

# life 单飞锁的 TTL：比一轮 life 思考的最坏耗时更大的上界（LLM 几十秒级）。锁只
# 是基建并发控制（不替 agent 决策、不违反赤尾宪法），TTL 到期后哪怕原 holder
# 还在跑、新 holder 也能进，token-CAS 释放保证不误删别人的锁（见 single_flight）。
_LIFE_WAKE_LOCK_TTL_SECONDS = 600

# offline-model：异步后台思考用离线模型（见 feedback_model_selection），主对话
# 才用 gemini。trace_name 让这一轮 life 思考接进 langfuse。
_LIFE_WAKE_CFG = AgentConfig("life_wake", "offline-model", "life-wake")

# observed_at 用 ISO8601 UTC；展示层时区由读取方处理。
_TZ = UTC


class LifeDecision(BaseModel):
    """LLM 想完一轮的结构化产出。

    **没有 state_end_at / skip_until** —— 她不锁死"做到几点"。只描述此刻什么样，
    外加可选的"我想做什么"意图。``intent_summary`` 为空表示这一轮她没起意图（只是
    默默换了个状态），不回灌 world。
    """

    current_state: str = Field(description="她此刻在干嘛（自然语言）")
    response_mood: str = Field(description="此刻的情绪 / 回应基调")
    activity_type: str = Field(description="活动类型（sleep / study / rest / move ...）")
    intent_summary: str | None = Field(
        default=None,
        description="这一轮她想做什么（回灌 world 去裁决）；没起意图就留空",
    )


def _format_unread(unread: list[EventEnvelope]) -> str:
    """把未读 event 拼成她"刚感知到 / 想起的几件事"的文字，按发生时间顺序。

    只放 event 的客观可感形态（summary）+ 类型 + 发生时间——这些都是投进她信箱的、
    她够得着的信息，不含任何 world 全局视角。
    """
    lines = []
    for ev in unread:
        lines.append(f"- [{ev.kind}] {ev.occurred_at} {ev.summary}")
    return "\n".join(lines)


async def _think(
    *,
    persona_id: str,
    snapshot: object | None,
    unread: list[EventEnvelope],
    prompt_vars: dict,
) -> LifeDecision:
    """喂 LLM 想一轮，拿回结构化决策。

    输入严格限定为她自己的快照 + 她自己信箱未读（已在 ``prompt_vars`` 里拼好），
    不碰 world 全局快照。
    """
    return await Agent(_LIFE_WAKE_CFG).extract(  # type: ignore[return-value]
        LifeDecision,
        messages=[Message(role=Role.USER, content="此刻你感知到了这些，想一轮。")],
        prompt_vars=prompt_vars,
    )


@node
async def life_wake_node(arrived: EventArrived) -> None:
    """某姐妹被攒批唤醒，做一轮 life。同构跑三姐妹，persona 由 ``arrived`` 决定。

    **单飞命门（必改 2）**：一轮 life LLM 跑几十秒 > debounce 窗口（5s），期间来
    新 event 会 fire 第二轮 ``life_wake_node`` 并发。两轮并发会互相覆盖 LifeState、
    把 event 静默标已读丢掉。所以开头按 ``(lane, persona)`` 拿一把单飞锁，确保同
    ``(lane, persona)`` 同时只有一轮在跑；拿不到锁就 ``raise DebounceReschedule``，
    交给 debounce handler CAS 重排、稍后再试（这一批 event 不被吞掉）。锁是基建
    并发控制、不替 agent 决策，不违反赤尾宪法。
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
    """一轮 life 的实际编排（已在单飞锁内）：读未读 → 想 → 存 → 标已读。"""
    unread = await list_unread_events(lane=lane, persona_id=persona_id)
    if not unread:
        # 空唤醒（去重命中后的残留信号等）：不烧 LLM、不写快照、不标已读。
        logger.info("[life_wake] %s/%s woke with empty inbox, skip", lane, persona_id)
        return

    snapshot = await find_life_state(lane=lane, persona_id=persona_id)
    pc = await load_persona(persona_id)

    now = datetime.now(_TZ)
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

    decision = await _think(
        persona_id=persona_id,
        snapshot=snapshot,
        unread=unread,
        prompt_vars=prompt_vars,
    )

    observed_at = now.isoformat()
    await save_life_state(
        lane=lane,
        persona_id=persona_id,
        current_state=decision.current_state,
        response_mood=decision.response_mood,
        activity_type=decision.activity_type,
        observed_at=observed_at,
    )

    if decision.intent_summary:
        # intent_id 从 (lane, persona, 本轮读到的 event_ids) 派生 —— durable 边
        # 重投 / 重试同一批唤醒时产出同一个 intent_id，world 端按 intent_id 幂等
        # 消化，不会重复起意图。
        read_ids = [ev.event_id for ev in unread]
        seed = f"{lane}:{persona_id}:" + ",".join(sorted(read_ids))
        intent_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
        await raise_intent(
            lane=lane,
            intent_id=intent_id,
            persona_id=persona_id,
            summary=decision.intent_summary,
            occurred_at=observed_at,
        )

    # 标已读：只标本轮实际读到的那批 event_id（绝不按 persona 全标）。
    await mark_events_read(
        lane=lane,
        persona_id=persona_id,
        event_ids=[ev.event_id for ev in unread],
    )
    logger.info(
        "[life_wake] %s/%s thought a round: state=%r mood=%r intent=%s, marked %d read",
        lane, persona_id, decision.current_state, decision.response_mood,
        bool(decision.intent_summary), len(unread),
    )
