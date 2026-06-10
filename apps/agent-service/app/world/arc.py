"""世界长弧快照 — 活的世界（时间常数分层）的慢层.

world 的自产状态分两层钟、两张表：``WorldState`` 写「此刻的世界是什么样」（每轮
都可能重写，明天就过时）；``WorldArc`` 写「跨周月仍然成立的世界进展」——「世界的
长弧走到哪」的自然语言全文快照，只在 world 推演判断到翻页级转变（考完 / 放榜 /
搬家 / 换季）时整篇重写。判据一句话：这句话下周还成立吗？成立才配进长弧。

每次更新都是整篇重写（翻过去的页不留在长弧里：高考结束**取代**备考，不是排在
备考后面），历史靠 append-only 版本链留痕、读侧只读最新一版（和 WorldState 同一个
as_latest 模式）。Key 带 lane（泳道隔离命门同 WorldState：coe / ppe 绝不能覆盖
prod 的世界长弧）。

写入走 framework 的 ``insert_append``（Version 自增），读走 ``select_latest``
（每个 key 取最新一版）——不绕开 framework 持久化原语。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest


class WorldArc(Data):
    """「世界的长弧走到哪」的自然语言全文快照.

    自然键 ``lane``（每个泳道一条长弧）。``narrative`` 是整篇重写的长弧全文——
    自然语言、不拆结构化表，写到谁就是谁（不建 per-persona 归属）。``turned_at``
    是翻页时的世界时刻（CST ISO8601），供将来记忆沉淀 / review 定位——命名避开
    框架保留列（``created_at`` 是框架的落库时刻，语义不同且是保留列）。
    ``version`` 让同一 lane 的多版长弧 append-only 保留历史、读最新一版。
    """

    lane: Annotated[str, Key]
    narrative: str   # 长弧全文（world 整篇重写的自然语言）
    turned_at: str   # 翻页时的世界时刻 (CST ISO8601)
    version: Annotated[int, Version] = 0


async def write_world_arc(*, lane: str, narrative: str, turned_at: str) -> None:
    """append 一版世界长弧（world 推演判断到翻页级转变时整篇重写）。

    durable 语义与 update_world 相同：append 新版本、无 dedup。整轮失败重跑可能
    再 append 一次语义相同的版本——无害，读侧只认最新版，版本链留痕。
    """
    await insert_append(
        WorldArc(lane=lane, narrative=narrative, turned_at=turned_at)
    )


async def read_world_arc(*, lane: str) -> WorldArc | None:
    """读某泳道最新一版世界长弧，没有返回 None（冷启动：长弧还是空白）。"""
    return await select_latest(WorldArc, {"lane": lane})
