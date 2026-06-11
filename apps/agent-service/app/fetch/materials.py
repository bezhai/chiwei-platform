"""DailyMaterials —— 每天一份外部底料简报，落 durable PG（刀 3 Task2）。

通用抓取 agent 每天清晨用三个结构化查询 skill 自己去查、自己把今天的外部底料组织成
一段「今天的客观底料」中文话，落进这张表，给 world 后续读它当背景知识。表只存 agent
组织好的那段话——每源是否拿到、是什么，agent 都已在这段话里说清（某源没拿到它会如实
说「今天没拿到」），world 直接读这段话，不需要每源的原始文本 / 成功标志。

一份底料 = ``briefing``（agent 组织的底料话）+ ``date`` + ``lane`` + ``fetched_at``。

自然键 (lane, date)：
  * ``lane`` —— 泳道隔离硬约束（同其它 durable Data：runtime 持久化不自动加 lane，
    不显式带上 coe / ppe 就会污染 prod 的底料）。
  * ``date`` —— 当天日期（CST「今天」，``YYYY-MM-DD``），让每个泳道每天唯一一份。

用 ``insert_idempotent``（非 ``insert_append``）：同一天若被重投 / 重试再抓一次，用
同一 ``(lane, date)`` 再写一次，``insert_idempotent`` 是 ON CONFLICT DO NOTHING、第
一次为准、不覆盖（一天就该只有一份底料、天然幂等）。没有 ``Version``——一天的底料是个
确定事实、不需要版本演进（对齐 sibling :class:`app.domain.thinking_cost.ThinkingTokensSpent`）。

字段都是标量（str），是这张表的形态选择——只存 agent 组织好的那段 briefing 话
（framework 已支持 dict/list → JSONB，这里不放结构化字段是设计、不是限制）。时刻
字段叫 ``fetched_at`` 而非 ``created_at`` / ``updated_at``——后者是 migrator 自动加的
保留列，业务字段绕开（对齐 ``ThinkingTokensSpent.observed_at`` 的口径）。
"""

from __future__ import annotations

import logging
from typing import Annotated

# insert_idempotent / select_latest imported module-level so tests can monkeypatch.
from app.runtime.data import Data, Key
from app.runtime.persist import insert_idempotent, select_latest

logger = logging.getLogger(__name__)


class DailyMaterials(Data):
    """某泳道某天的外部底料简报。自然键 (lane, date)，按天幂等。

    只存抓取 agent 组织好的那段「今天的客观底料」中文话（``briefing``）+ 抓取时刻。
    world 读 ``briefing`` 当背景知识——某源是否拿到 agent 已在这段话里说了。
    """

    lane: Annotated[str, Key]
    date: Annotated[str, Key]          # CST「今天」(YYYY-MM-DD)
    briefing: str                      # agent 组织的「今天的客观底料」中文话
    fetched_at: str                    # 这份底料的抓取时刻 (CST aware ISO8601)


async def save_daily_materials(
    *,
    lane: str,
    date: str,
    briefing: str,
    fetched_at: str,
) -> None:
    """把今天的底料落 ``DailyMaterials``（按天幂等：同一天重投只落第一份）。

    用 ``insert_idempotent``：同一 ``(lane, date)`` 再抓一次（重投 / 重试）走 ON
    CONFLICT DO NOTHING、第一次为准、不覆盖也不报错。
    """
    await insert_idempotent(
        DailyMaterials(
            lane=lane,
            date=date,
            briefing=briefing,
            fetched_at=fetched_at,
        )
    )


async def find_daily_materials(*, lane: str, date: str) -> DailyMaterials | None:
    """读某泳道某天的底料，没抓过返回 None（world 据此知道今天还没底料）。"""
    return await select_latest(DailyMaterials, {"lane": lane, "date": date})
