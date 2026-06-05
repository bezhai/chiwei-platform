"""客观世界叙述快照 — 阶段 1A（world 推演者）.

world 只剩一块客观状态：``WorldState`` —— 这家的客观世界叙述快照（``world_time``
+ 一段自然语言 ``detail`` 叙述），as_latest（append-only + 读最新一版）、Key 带
lane（泳道隔离命门：coe / ppe 绝不能覆盖 prod 的"她此刻客观世界"）。

新范式下没有 presence 表了。旧设计里 world 是导演——拿 ``RoomPresence`` 查表记
"谁在哪个房间"、按同-room 机械匹配投递。现在 world 是推演者：世界此刻什么样、
谁大概在哪在干嘛，全融进 ``detail`` 自然语言叙述里，由 world 推演维护；谁够得着
一条动静也由 world 推演（不查表）。所以这里只剩世界叙述一块快照，没有任何结构化
在场表。

写入走 framework 的 ``insert_append``（Version 自增），读走 ``select_latest``
（每个 key 取最新一版）——不绕开 framework 持久化原语。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest


class WorldState(Data):
    """这家的客观世界叙述快照：世界时间 + 一段自然语言叙述。

    自然键 ``lane``（每个泳道一份客观世界）。``world_time`` / ``detail`` 都是
    TEXT —— framework persist 层暂不能持久化结构化字段，所以快照里只放可读文本。
    ``detail`` 是 world 推演出来的"世界此刻什么样"（谁大概在哪、在干嘛、什么氛围，
    位置融在叙述里），``version`` 让同一 lane 的多版快照 append-only 保留历史、读
    最新一版。
    """

    lane: Annotated[str, Key]
    world_time: str  # 世界此刻 (ISO8601)
    detail: str      # 世界此刻的客观叙述（world 推演出来的一段自然语言）
    version: Annotated[int, Version] = 0


async def write_world_state(*, lane: str, world_time: str, detail: str) -> None:
    """append 一版客观世界叙述快照（world 用 update_world 工具推演完落最新世界状态）。"""
    await insert_append(
        WorldState(lane=lane, world_time=world_time, detail=detail)
    )


async def read_world_state(*, lane: str) -> WorldState | None:
    """读某泳道客观世界最新一版叙述快照，没有返回 None（冷启动）。"""
    return await select_latest(WorldState, {"lane": lane})
