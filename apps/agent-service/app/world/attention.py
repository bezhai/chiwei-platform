"""世界的关注快照 — world 的眼睛（感官闭环）的「想看哪」.

world 的自产状态各落各的链、语义不混放：``WorldArc`` 写「世界的长弧**走到哪**」
（跨周月仍成立的世界进展），``WorldAttention`` 写「世界当前**想看哪**」——反思
对表时留给眼睛的关切（在等什么消息、想确认什么事），次晨眼睛带着它去看、把看到
的带回底料、再由反思消化决定续看还是清掉。闭环节拍天然一天一圈。

每次更新都是整篇重写（写「当前仍想看的」：看完的关注被新版**取代**，不是追加成
清单），历史靠 append-only 版本链留痕、读侧只读最新一版（和 WorldArc 同一个
as_latest 模式）。**清空也是一版**：append-only 链没有删除态，反思判断当下没有
要看的，就重写一版说明「没有特别要看的」取代旧关注——不写这一版，旧关注会被
眼睛永远读下去。Key 带 lane（泳道隔离命门同 WorldArc：coe / ppe 绝不能覆盖
prod 的关注）。

写入走 framework 的 ``insert_append``（Version 自增），读走 ``select_latest``
（每个 key 取最新一版）——不绕开 framework 持久化原语。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest


class WorldAttention(Data):
    """「世界当前想看哪」的自然语言全文快照.

    自然键 ``lane``（每个泳道一版当前关注）。``narrative`` 是整篇重写的关注全文——
    自然语言、不拆结构化分类字段（关注是 agent 的判断，不建信息源注册表）。
    ``written_at`` 是反思写下这版关注的世界时刻（CST ISO8601），供眼睛看到关注
    是哪天留的——命名避开框架保留列（``created_at`` 是框架的落库时刻，语义不同
    且是保留列）。``version`` 让同一 lane 的多版关注 append-only 保留历史、读
    最新一版。
    """

    lane: Annotated[str, Key]
    narrative: str    # 关注全文（反思整篇重写的自然语言；清空版也是一版全文）
    written_at: str   # 反思写下这版关注的世界时刻 (CST ISO8601)
    version: Annotated[int, Version] = 0


async def write_world_attention(
    *, lane: str, narrative: str, written_at: str
) -> None:
    """append 一版世界关注（反思对表时整篇重写「当前仍想看的」，清空也是一版）。

    durable 语义与 write_world_arc 相同：append 新版本、无 dedup。整轮失败重跑
    可能再 append 一次语义相同的版本——无害，读侧只认最新版，版本链留痕。
    """
    await insert_append(
        WorldAttention(lane=lane, narrative=narrative, written_at=written_at)
    )


async def read_world_attention(*, lane: str) -> WorldAttention | None:
    """读某泳道最新一版世界关注，没有返回 None（冷启动：眼睛只做本能扫视）。"""
    return await select_latest(WorldAttention, {"lane": lane})
