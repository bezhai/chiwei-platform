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
    # **dead field（Task 2 删自设闹钟后保留的死列，恒为 None）。** 这列曾承载「她自排
    # 下次几点醒」的闹钟意愿，但纯客观事件驱动范式把 life 自设闹钟整条删了——闹钟是个
    # 空时间点、目的只是维持她运转，设错 / 丢失就睡死，存活不该压在她自己手上。她的存活
    # 改由世界持续的客观事件流兜底、主动计划改走日程（notebook + 到点提醒）。Task 1 收口
    # 后 world 也不再读它判谁该醒，所以这列已无任何读 / 写方。
    #
    # **为什么列还留着、不删**：framework Data migrate 是 fail-closed，删列会让整批
    # 回滚 → pod crash loop（coe 有旧表必踩）。删列是单独的 migrate 决策，本次不动列，
    # 只删掉所有写入路径让它变成恒 None 的 dead field。nullable additive 列，默认 None。
    next_wake_at: str | None = None
    # 「最近一次睡前回顾的目标生活日」标签（[04:00, 次日 04:00) 的 YYYY-MM-DD，
    # 见 app/life/living_day.py）。回顾**成功**才由 mark_day_reviewed 落进来。
    # **已降级为观测留痕、绝不当闸读**（2026-06-12 prod 事故：单字段回答不了
    # 「某一天回顾过没有」——清晨回笼觉的快班把它推前到新生活日，对账班比对它
    # 误判前一日未回顾、重跑出重复页）：「那天回顾过没有」的权威口径是
    # data_day_page 该 (lane, persona, date) 的页是否存在（app/life/pages.py 的
    # day_page_exists）。列保留照写——framework Data migrate 是 fail-closed，
    # 删列会让 pod crash loop；life_wake 注入「她最近一页昨天」仍把它当指针用
    # （指最近回顾过的生活日，不是闸）。nullable additive migrate（对齐
    # next_wake_at）；默认 None 不撞 migrator 保留列。命门同 arc_reflected_date
    # 教训：LifeState 的**每个写点**都要沿用它（Task 2 删自设闹钟后写点只剩
    # save_life_state），否则一轮 update 就把留痕静默清掉。
    day_reviewed_date: str | None = None


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

    **``next_wake_at`` 是 dead field（Task 2 删自设闹钟后恒为 None）**：自设闹钟整条
    删掉后没有任何写入方，新版照常写 ``None``（列保留、不删）。

    **沿用上一版的 ``day_reviewed_date``（必改命门）**：它是睡前回顾的观测留痕，
    LifeState append-only、每版整行重写，本函数只改主观快照（在干嘛 / 情绪 / 活动），
    若不沿用就会把留痕静默清掉 → 同生活日重跑回顾。所以读最新一版、沿用它的
    ``day_reviewed_date``，与 :func:`mark_day_reviewed` 沿用主观字段对称。
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
            day_reviewed_date=prev.day_reviewed_date if prev is not None else None,
        )
    )


async def find_life_state(*, lane: str, persona_id: str) -> LifeState | None:
    """读某姐妹在某泳道的最新主观快照，没有则 ``None``（她还没活过一轮）。"""
    snap = await select_latest(LifeState, {"lane": lane, "persona_id": persona_id})
    return snap  # type: ignore[return-value]


async def mark_day_reviewed(*, lane: str, persona_id: str, date: str) -> None:
    """睡前回顾**成功**后把「最近回顾的生活日」留痕落成 ``date``（生活日 YYYY-MM-DD）。

    **观测留痕、不是闸**（2026-06-12 事故后降级）：两班判「那天回顾过没有」一律
    看 data_day_page 该日页的存在性（day_page_exists），绝不比对这个单字段——
    它只剩两个用途：排查时看一眼最近回顾到哪天、life_wake 注入「她最近一页
    昨天」时当指针。本函数只在回顾 Agent 调用成功（昨天页核验存在）后被调，
    保持「成功才落、保留其余字段」的写法（与 mark_arc_reflected 同构）。

    LifeState 是 append-only：读最新一版、沿用其余主观快照各字段（在干嘛 / 情绪 /
    活动 / 观测时刻都不丢），只换 ``day_reviewed_date``，append 一版。``next_wake_at``
    是 Task 2 删自设闹钟后的 dead field，新版照常写 None（不再沿用）。

    冷启容错：还没有任何 LifeState 快照（她从没活过一轮）时安全跳过、不造占位
    假状态——空字符串占位快照会被 life 轮的冷启恢复段当成"上次记得自己在做："
    喂出怪话；留痕缺席无害（重跑防护本就不靠它）。
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
            day_reviewed_date=date,
        )
    )
