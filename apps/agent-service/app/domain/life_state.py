"""LifeState — 某姐妹"此刻"的主观快照 (Task 3, life engine 三姐妹).

赤尾世界里三姐妹（akao / chinagi / ayana）各自有一份主观快照：她现在在干嘛、
什么情绪、活动类型 + 这份快照是何时观测到的。这是 chat / world / life
读"她当前状态"的唯一来源。

设计上钉死的两条：

  * **没有 ``state_end_at``。** 旧 life engine 在状态里塞一个"做到几点"，没到期
    就 ``return None`` 干等，中途任何 event 进不来——她卡在"去上学的路上"。新快照
    只描述"此刻什么样"，不含任何"做到几点"的死时间段。她什么时候换状态，由下一条
    event 把她推醒、她重想一轮决定，不由快照里的闹钟决定。

  * **as_latest + Version，Key 带 lane。** 每想完一轮 ``insert_append`` 一版，
    对外读永远 ``select_latest`` 取最新那版（旧版留作历史，不删）。Key 含 lane——
    runtime 持久化不会自动加 lane，不显式带上，coe / ppe 泳道就会覆盖 prod 的
    "她此刻状态"（写脏线上客观真相）。

字段都是 str：``current_state`` / ``response_mood`` / ``activity_type`` +
``observed_at``（ISO8601）。主观快照本就只需要这几个标量字段，是它的形态选择
（framework 已支持 dict / list → JSONB 持久化，这里不放结构化字段是设计、不是限制）。

``observed_at`` 而非 ``updated_at``：``updated_at`` 是 runtime 保留列（migrator
自动加），不能拿来当业务字段名。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest


class LifeState(Data):
    """某姐妹此刻的主观快照。as_latest（带 Version），Key = (lane, persona_id)。

    一份快照 = 她此刻在干嘛 + 什么情绪 + 活动类型 + 观测时刻。**无 state_end_at**：
    换状态靠下个 event 推醒重想，不靠快照里的闹钟。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    current_state: str       # 她此刻在干嘛（自然语言）
    response_mood: str       # 此刻的情绪 / 回应基调
    activity_type: str       # 活动类型（sleep / study / rest / move ...）
    observed_at: str         # 这份主观快照观测到的时刻 (ISO8601)
    # 下次该醒的**现实**时刻（CST aware ISO，阶段 1B Task 2 到点 gate）。她调 schedule
    # 自排后由收口 fire_life_self_wake 写进来；她的自排唤醒入口（LifeWakeTick reason=self）
    # 走 gate 时读它判到点 + stale。nullable：从没自排过（首轮 / 只被 notify 起头）时为
    # None。framework migrate 对已有数据的表加 nullable 列是 additive、不阻塞。比较一律用
    # 现实时间，对称 world WorldState.next_wake_at。
    next_wake_at: str | None = None


async def save_life_state(
    *,
    lane: str,
    persona_id: str,
    current_state: str,
    response_mood: str,
    activity_type: str,
    observed_at: str,
) -> None:
    """想完一轮 → append 一版新的主观快照。

    ``insert_append`` 自动递增 ``ver``；旧版留作历史。对外读用
    :func:`find_life_state` 取最新一版。

    **沿用上一版的 ``next_wake_at``（必改 2 命门）**：``next_wake_at`` 是她的自排意愿
    （何时再醒），由 ``schedule`` 工具收口的 :func:`set_life_next_wake_at` 负责写。
    update_life_state（→ 本函数）只改主观快照（在干嘛 / 情绪 / 活动），**绝不动自排
    意愿**。若这里默认把 ``next_wake_at`` 清成 None，event 唤醒她、她 update 但没重新
    schedule 时，之前排的 next_wake_at 被清 → 旧 self wake 到期被 gate 判 stale（携带
    target != None）作废 → 她不再自排醒、链断、回到等 event。所以 append 时读最新一版、
    沿用它的 ``next_wake_at``，与 :func:`set_life_next_wake_at` 沿用主观字段对称：两个
    写路径各改各字段、沿用对方最新值，只有 schedule（set）能改 next_wake_at。
    """
    prev = await find_life_state(lane=lane, persona_id=persona_id)
    await insert_append(
        LifeState(
            lane=lane,
            persona_id=persona_id,
            current_state=current_state,
            response_mood=response_mood,
            activity_type=activity_type,
            observed_at=observed_at,
            next_wake_at=prev.next_wake_at if prev is not None else None,
        )
    )


async def find_life_state(*, lane: str, persona_id: str) -> LifeState | None:
    """读某姐妹在某泳道的最新主观快照，没有则 ``None``（她还没活过一轮）。"""
    snap = await select_latest(LifeState, {"lane": lane, "persona_id": persona_id})
    return snap  # type: ignore[return-value]


async def set_life_next_wake_at(
    *, lane: str, persona_id: str, next_wake_at: str
) -> None:
    """记下某姐妹下次该醒的现实时刻（阶段 1B Task 2 到点 gate，对称 world set_next_wake_at）。

    她调 schedule 决定下次几时醒后，由收口 :func:`app.nodes.life_tools.fire_life_self_wake`
    把目标唤醒时刻（现实 now + schedule 秒数）写进来。LifeState 是 append-only（带
    ``ver`` Version）：这里读最新一版、沿用它的主观快照各字段（current_state /
    response_mood / activity_type / observed_at，不丢状态），只把 ``next_wake_at``
    换成新目标，append 一版。双键 (lane, persona_id) —— 只动这一个 persona 的 state。

    冷启容错：还没有任何 LifeState 快照（她从没活过一轮）时无可承载 next_wake_at 的
    快照，安全跳过（不造假状态占位）。这种情形下 next_wake_at 没排上，靠 world 在
    饭点 / 早晨的 notify 起头兜底——不抛、不卡死。
    """
    snapshot = await find_life_state(lane=lane, persona_id=persona_id)
    if snapshot is None:
        return
    await insert_append(
        LifeState(
            lane=lane,
            persona_id=persona_id,
            current_state=snapshot.current_state,
            response_mood=snapshot.response_mood,
            activity_type=snapshot.activity_type,
            observed_at=snapshot.observed_at,
            next_wake_at=next_wake_at,
        )
    )
