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
    # 下次该醒的**现实**时刻（CST aware ISO，阶段 1B 到点 gate）。world 调 sleep 后
    # 由收口 fire_self_wake 写进来；world 唤醒入口对 self / 心跳走 gate 时读它判到点。
    # nullable：从没排过下次醒（首轮 / 只 update_world 没 sleep）时为 None，心跳放行
    # 别卡死。framework migrate 对已有数据的表加 nullable 列是 additive、不阻塞。比较
    # 一律用现实时间、不用 world_time（world_time 会因 gate 停滞）。
    next_wake_at: str | None = None
    # act 消费游标（pull 范式）：上次推完一批 act 推进到的复合游标
    # ``(created_at, act_id)``。world 醒来读"游标之后的 act"，推完把游标推进到本批
    # 末尾。游标用 ``created_at``（framework 给每行自动加的单调落库时刻）而非
    # ``occurred_at``（life 轮首固定的做事时刻、与落库顺序可乱序）——按 occurred_at
    # 推进会先消费"晚发生但早落库"的 act 把游标推过去、之后落库的"早发生" act 永远
    # 读不到（漏 act）；created_at 单调落库序、按它推进不漏（命门见 acts.py docstring）。
    # 复合游标是因为同一落库瞬间理论上可能多条 act：只用 created_at 的 ``>`` 漏边界同刻
    # 新行、``>=`` 重读边界旧行；加 act_id 作稳定 tie-breaker 两头都不错。nullable：从没
    # 消费过（冷启动）时为 None，读全既有 act。两列同样是 additive nullable migrate。
    act_cursor_created_at: str | None = None
    act_cursor_act_id: str | None = None
    version: Annotated[int, Version] = 0


async def write_world_state(*, lane: str, world_time: str, detail: str) -> None:
    """append 一版客观世界叙述快照（world 用 update_world 工具推演完落最新世界状态）。

    update_world 工具在循环中途调，只更新世界叙述（world_time + detail），**保留**
    收口才写的调度状态（``next_wake_at`` / act 游标）。否则每轮更新叙述都把游标 /
    到点时刻打回 None，下轮重读全部 act / 长睡意愿失效。WorldState 是 append-only：
    读最新一版、沿用它的调度状态，只换 world_time / detail，append 一版。冷启动
    （首版还没快照）时无可沿用的调度状态，照常以 None 起。
    """
    prev = await read_world_state(lane=lane)
    await insert_append(
        WorldState(
            lane=lane,
            world_time=world_time,
            detail=detail,
            next_wake_at=prev.next_wake_at if prev is not None else None,
            act_cursor_created_at=prev.act_cursor_created_at if prev is not None else None,
            act_cursor_act_id=prev.act_cursor_act_id if prev is not None else None,
        )
    )


async def read_world_state(*, lane: str) -> WorldState | None:
    """读某泳道客观世界最新一版叙述快照，没有返回 None（冷启动）。"""
    return await select_latest(WorldState, {"lane": lane})


async def set_next_wake_at(*, lane: str, next_wake_at: str) -> None:
    """记下 world 下次该醒的现实时刻（阶段 1B 到点 gate）。

    world 调 sleep 决定下次几时醒后，由收口 :func:`app.world.tools.fire_self_wake`
    把目标唤醒时刻（现实 now + sleep 秒数）写进来。WorldState 是 append-only：这里
    读最新一版、沿用它的 ``world_time`` / ``detail`` / act 游标（不丢世界叙述、不丢
    已消费游标），只把 ``next_wake_at`` 换成新目标，append 一版。

    冷启容错：还没有任何 WorldState 快照（首轮还没 update_world 落叙述）时无可承载
    next_wake_at 的快照，安全跳过（不造空 detail 占位，世界叙述统一由 update_world
    工具落）。这种情形下 next_wake_at 没排上，靠保底心跳兜底——不抛、不卡死。
    """
    snapshot = await read_world_state(lane=lane)
    if snapshot is None:
        return
    await insert_append(
        WorldState(
            lane=lane,
            world_time=snapshot.world_time,
            detail=snapshot.detail,
            next_wake_at=next_wake_at,
            act_cursor_created_at=snapshot.act_cursor_created_at,
            act_cursor_act_id=snapshot.act_cursor_act_id,
        )
    )


async def advance_act_cursor(*, lane: str, created_at: str, act_id: str) -> None:
    """把 act 消费游标推进到本批末尾（pull 范式收口）。

    world 推完一批 act 成功收口后调本函数，把复合游标 ``(created_at, act_id)`` 推到
    本批最后一条 act 的坐标——下轮只读它之后的 act，不重读这批。``created_at`` 是该行
    framework 自动写的单调落库时刻（不是 occurred_at），按它推进不漏（命门见 acts.py）。
    WorldState 是 append-only：读最新一版、沿用它的 ``world_time`` / ``detail`` /
    ``next_wake_at``（不丢世界叙述、不丢到点时刻），只换 act 游标，append 一版。

    冷启容错：还没有任何 WorldState 快照（首版还没 update_world 落叙述）时无可承载
    游标的快照，安全跳过——这种情形下本就没消费到任何 act（冷启动批次推演里 world
    会先 update_world 落第一版），下次再推进。不抛、不卡死。
    """
    snapshot = await read_world_state(lane=lane)
    if snapshot is None:
        return
    await insert_append(
        WorldState(
            lane=lane,
            world_time=snapshot.world_time,
            detail=snapshot.detail,
            next_wake_at=snapshot.next_wake_at,
            act_cursor_created_at=created_at,
            act_cursor_act_id=act_id,
        )
    )
