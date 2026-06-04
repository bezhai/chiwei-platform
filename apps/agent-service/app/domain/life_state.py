"""LifeState — 某姐妹"此刻"的主观快照 (Task 3, life engine 三姐妹).

赤尾世界里三姐妹（akao / chinagi / ayana）各自有一份主观快照：她现在在干嘛、
什么情绪、活动类型 + 这份快照是何时观测到的。这是 chat / 整点语音 / reviewer
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
``observed_at``（ISO8601）。framework 当前 durable Data 不能用 dict / list
字段（无 JSONB 持久化编解码），主观快照本就只需要这几个标量字段，不撞这个 gap。

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
    """
    await insert_append(
        LifeState(
            lane=lane,
            persona_id=persona_id,
            current_state=current_state,
            response_mood=response_mood,
            activity_type=activity_type,
            observed_at=observed_at,
        )
    )


async def find_life_state(*, lane: str, persona_id: str) -> LifeState | None:
    """读某姐妹在某泳道的最新主观快照，没有则 ``None``（她还没活过一轮）。"""
    snap = await select_latest(LifeState, {"lane": lane, "persona_id": persona_id})
    return snap  # type: ignore[return-value]
