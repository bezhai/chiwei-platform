"""客观世界快照 + 房间级在场 — Task 2 (world engine).

world 维护两块客观状态，都是 as_latest（append-only + 读最新一版）、Key 带
lane（泳道隔离命门：coe / ppe 绝不能覆盖 prod 的"她此刻客观世界"）：

  * :class:`WorldState` —— 这家的客观世界快照：世界时间 + 客观情形描述。
    每次 world 推演完 append 一版，下次唤醒读最新那版当起点。
  * :class:`RoomPresence` —— 房间级在场，**拍平成每个 persona 一行**
    （Key=(lane, persona_id)，载 room_id）。

为什么在场拍平、不塞一个 ``dict`` 字段：framework 的 persist 层（insert_append /
insert_idempotent）暂不能把 dict / list 字段序列化进 JSONB 列（无 json 编解码，
放 dict 会 asyncpg DataError，见 world_events.py 的 capability gap 注）。所以
"谁在哪个房间"不做成一个结构化 dict 字段，而是一 persona 一行的 ``room_id``，
干净地落进 TEXT 列、按 (lane, persona) as_latest 查回最新位置。

写入走 framework 的 ``insert_append``（Version 自增），读走 ``select_latest``
（每个 key 取最新一版）——不绕开 framework 持久化原语。
"""

from __future__ import annotations

from typing import Annotated

from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, Key, Version
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_append, select_latest


class WorldState(Data):
    """这家的客观世界快照：世界时间 + 客观情形。

    自然键 ``lane``（每个泳道一份客观世界）。``world_time`` / ``situation``
    都是 TEXT —— framework persist 层暂不能持久化结构化字段，所以快照里只放
    可读文本，结构化的在场拆到 :class:`RoomPresence`。``version`` 让同一 lane
    的多版快照 append-only 保留历史、读最新一版。
    """

    lane: Annotated[str, Key]
    world_time: str  # 世界此刻 (ISO8601)
    situation: str   # 客观情形描述（喂 world LLM 推演的起点）
    version: Annotated[int, Version] = 0


class RoomPresence(Data):
    """房间级在场，拍平成每 persona 一行。

    自然键 ``(lane, persona_id)``：某 persona 在某泳道里此刻在哪个房间。位置
    变更 append 新版，读最新一版即她当前房间。拍平而非 dict 字段，是 framework
    persist 层暂不能持久化结构化负载的干净绕法（见模块 docstring）。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    room_id: str
    version: Annotated[int, Version] = 0


async def write_world_state(*, lane: str, world_time: str, situation: str) -> None:
    """append 一版客观世界快照（world 推演完落最新世界状态）。"""
    await insert_append(
        WorldState(lane=lane, world_time=world_time, situation=situation)
    )


async def read_world_state(*, lane: str) -> WorldState | None:
    """读某泳道客观世界最新一版快照，没有返回 None（冷启动）。"""
    return await select_latest(WorldState, {"lane": lane})


async def set_presence(*, lane: str, persona_id: str, room_id: str) -> None:
    """设某 persona 当前在哪个房间（位置变更 append 新版）。"""
    await insert_append(
        RoomPresence(lane=lane, persona_id=persona_id, room_id=room_id)
    )


async def read_presence(*, lane: str, persona_id: str) -> str | None:
    """读某 persona 当前房间；没设过在场返回 None（不在场）。"""
    latest = await select_latest(
        RoomPresence, {"lane": lane, "persona_id": persona_id}
    )
    return latest.room_id if latest else None


_PRESENCE_TABLE = _table_name(RoomPresence)


async def personas_in_room(*, lane: str, room_id: str) -> list[str]:
    """读某房间当前在场的 persona 集合（产生侧在场过滤的依据）。

    在场拍平成每 persona 一行 + as_latest，所以"谁在这屋"= 每个 persona 取
    她最新一版 RoomPresence、room_id 命中目标房间的那些。用 DISTINCT ON 取每
    persona 最新版（不绕开 framework 的写入，只在它写好的表上做一个 latest
    集合查询，口径与 select_latest 一致）。
    """
    sql = (
        f"SELECT persona_id FROM ("
        f"  SELECT DISTINCT ON (persona_id) persona_id, room_id "
        f"  FROM {_PRESENCE_TABLE} "
        f"  WHERE lane = :lane "
        f"  ORDER BY persona_id, version DESC"
        f") latest WHERE room_id = :room_id"
    )
    async with get_session() as s:
        result = await s.execute(text(sql), {"lane": lane, "room_id": room_id})
        return [row[0] for row in result.fetchall()]
