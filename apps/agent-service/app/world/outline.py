"""world 续写的工作记忆「大纲」(WorldOutline) — 世界此刻正在走的几条客观线.

world 续写每轮失忆地看一眼 detail（此刻快照）+ act（角色行动）就即兴推演，没有「世界
正在走哪几条线」的记忆——绫奈挂急诊后 detail 把「在医院做检查」写完，下一轮重写漏掉
「还在等结果」，这件事就从世界里蒸发了。大纲补这个洞：给续写一份**自己维护的工作记忆**，
记着世界此刻正在走的几条**未完成客观线**，每条写清「现在走到哪 + 客观上接下来怎么走 +
改写/结束条件」，续写沿着它把在跑的客观进程往前推到出结果。

大纲与既有两层 world 状态划清职责：

  * ``WorldState.detail`` —— 此刻的世界全图快照，每轮整体重写、明天就过时。漏写即丢线
    （绫奈急诊卡死的根因），所以「没办完的事」不赌它、由大纲承载。
  * ``WorldArc`` —— reflection 每日翻一次写的「跨周月底色」（换季/搬家/考完），续写
    **只读不碰**（工具集物理隔离掉了 update_arc）。
  * ``WorldOutline`` —— 续写**自己**维护的「活的 spec」：续写就是沿着它推进世界的，
    写和用是同一个脑子的同一件事，所以挂进续写工具集（``update_outline``），不学
    reflection 那套独立环节 + 独立钟。

内容契约（防止大纲职责往 detail / arc / life 漂的护栏）：大纲只写「未完成的客观线 +
每条现在走到哪（当前客观状态）+ 客观上下一步怎么走 + 这条线的改写/结束条件」，不放
现场描写（那是 detail）、不放主观感受（那是 life）、不放跨周月底色（那是 arc）。

结构照 ``WorldArc`` 的 append-only 版本链：每次更新都是整篇重写（办完的线被取代、不
排成历史流水账），历史靠版本链留痕、读侧只读最新一版（as_latest，同 WorldState /
WorldArc）。Key 带 lane（泳道隔离命门：coe / ppe 绝不能覆盖 prod 的大纲）。

写入走 framework 的 ``insert_append``（Version 自增），读走 ``select_latest``
（每个 key 取最新一版）——不绕开 framework 持久化原语。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest


class WorldOutline(Data):
    """world 续写的工作记忆「大纲」：世界此刻正在走的几条未完成客观线的全文快照.

    自然键 ``lane``（每个泳道一份大纲）。``narrative`` 是整篇重写的大纲全文——每条线
    写「① 这条未完成的客观线是什么 + ② 现在走到哪（当前客观状态）+ ③ 客观上接下来
    大致怎么走 + ④ 这条线的改写/结束条件」，办完的线（出了结果）从大纲里结掉。内容
    契约划清边界：不放现场描写（那是 ``WorldState.detail``）、不放主观感受（那是
    life）、不放跨周月底色（那是 ``WorldArc``）。``outlined_at`` 是梳理这版大纲的
    世界时刻（CST ISO8601），语义对齐 ``WorldArc.turned_at``——命名避开框架保留列
    （``created_at`` 是框架的落库时刻，语义不同且是保留列）。``version`` 让同一 lane
    的多版大纲 append-only 保留历史、读最新一版。
    """

    lane: Annotated[str, Key]
    narrative: str      # 大纲全文（续写整篇重写的自然语言：几条在走的客观线）
    outlined_at: str    # 梳理这版大纲的世界时刻 (CST ISO8601)
    version: Annotated[int, Version] = 0


async def write_world_outline(
    *, lane: str, narrative: str, outlined_at: str
) -> None:
    """append 一版大纲（续写判断该改时整篇重写它）。

    durable 语义与 update_world / update_arc 相同：append 新版本、无 dedup。整轮失败
    重跑可能再 append 一次语义相同的版本——无害，读侧只认最新版、版本链留痕。
    """
    await insert_append(
        WorldOutline(lane=lane, narrative=narrative, outlined_at=outlined_at)
    )


async def read_world_outline(*, lane: str) -> WorldOutline | None:
    """读某泳道最新一版大纲，没有返回 None（冷启动：大纲还是空白，prompt 引导续写补写）。"""
    return await select_latest(WorldOutline, {"lane": lane})
