"""昨天页 + 关系页版本链 — 睡前回顾（角色自己的慢钟）的两张落点.

睡前回顾以她本人第一人称回看刚结束的生活日，产出两样、各落各的链：``DayPage``
写「这一天在她心里留下的几笔」（按 persona + 生活日一条链），``RelationshipPage``
写「他与我」——当天聊过的每个真人一页（按 persona + 对方 user_id 一条链）。两张
都照 WorldArc 模板（第四、五次复用）：Key + narrative + written_at + Version、
append-only、读最新一版、整篇重写——新版**取代**旧版，不是追加成清单。

钉死的语义（数据层只管落点，纪律在 prompt 层）：

  * 昨天页的 ``date`` 是「生活日」标签（[当日 04:00, 次日 04:00) 的日期，与眼睛
    的晨界同一口径），由调用方按钟的约定算好传入——本模块不管边界，只把它当
    Key 字符串。同一生活日重写是常态（入睡快班写过、凌晨对账班再写），版本链留痕。
  * 关系页的 ``other_user_id`` 是跨群稳定的用户标识（username 可改名、不做键，
    页内容里自然写名字）。**没有删除态**：关系淡了就在整篇重写里自然淡出，
    append-only 链全留历史。页有篇幅感（一页之内、旧的让位新的）——这条纪律
    在工具说明与 prompt 层钉，数据层不设长度闸（赤尾宪法：不用确定性规则
    消除不确定性）。
  * 批量读 :func:`read_relationship_pages`：回顾本体按「当天聊过的人」逐个取
    旧页喂证据，没页的人不在返回里（缺席不补占位——第一次聊的人是常态）。

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


class DayPage(Data):
    """「这一天在她心里留下的几笔」的自然语言全文快照.

    自然键 ``(lane, persona_id, date)``：泳道隔离（coe / ppe 绝不能覆盖 prod 的
    页）+ 每人每生活日一条链。``date`` 是生活日标签（YYYY-MM-DD，调用方算好
    传入）。``narrative`` 是整篇重写的昨天页全文——留下来的几笔、不是流水账
    （纪律在 prompt 层）。``written_at`` 是写下这页的现实时刻（CST ISO8601）——
    命名避开框架保留列（``created_at`` 是框架的落库时刻，语义不同且是保留列，
    同 WorldArc 的 turned_at 教训）。``version`` 让同一生活日的多版页
    append-only 保留历史、读最新一版。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    date: Annotated[str, Key]   # 生活日标签 YYYY-MM-DD（[04:00, 次日 04:00)，调用方算好）
    narrative: str              # 昨天页全文（她整篇重写的自然语言）
    written_at: str             # 写下这页的现实时刻 (CST ISO8601)
    version: Annotated[int, Version] = 0


class RelationshipPage(Data):
    """「他与我」的自然语言全文快照 —— 她对一个真人的关系页.

    自然键 ``(lane, persona_id, other_user_id)``：每个姐妹对每个真人各有各的
    一页（persona 进 Key），``other_user_id`` 用跨群稳定的用户标识。``narrative``
    是整篇重写的关系页全文——写关系不写档案、自然语言不拆字段（纪律在 prompt
    层）。``written_at`` / ``version`` 语义同 :class:`DayPage`。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    other_user_id: Annotated[str, Key]   # 对方的跨群稳定用户标识（username 可改名不做键）
    narrative: str                       # 「他与我」全文（她整篇重写的自然语言）
    written_at: str                      # 写下这页的现实时刻 (CST ISO8601)
    version: Annotated[int, Version] = 0


async def write_day_page(
    *, lane: str, persona_id: str, date: str, narrative: str, written_at: str
) -> None:
    """append 一版昨天页（睡前回顾对这个生活日整篇重写）。

    durable 语义同 write_world_arc：append 新版本、无 dedup。整次回顾失败重跑
    可能再 append 一次语义相同的版本——无害，读侧只认最新版，版本链留痕。
    """
    await insert_append(
        DayPage(
            lane=lane,
            persona_id=persona_id,
            date=date,
            narrative=narrative,
            written_at=written_at,
        )
    )


async def read_day_page(
    *, lane: str, persona_id: str, date: str
) -> DayPage | None:
    """读某人某生活日最新一版昨天页，没有返回 None（冷启动：她还没有昨天可忆）。"""
    return await select_latest(
        DayPage, {"lane": lane, "persona_id": persona_id, "date": date}
    )


async def day_page_exists(*, lane: str, persona_id: str, date: str) -> bool:
    """某人某**确切生活日**是否已有昨天页（任何版本算有），没有返回 False。

    清晨对账班「那天回顾过没有」的权威口径（2026-06-12 prod 事故修复）：单字段
    marker（LifeState.day_reviewed_date）会被清晨回笼觉的快班推前到新生活日，
    回答不了按天的问题——按天的事实只在这张表里。框架 ``select_latest`` 取的是
    整行页全文，这里只要存在性，照 acts.py 的先例在 framework 持久化写好的
    真实表上做一个只读 EXISTS 查询；写入仍走 ``insert_append``，不绕开
    framework 持久化原语。
    """
    sql = (
        f"SELECT 1 FROM {_table_name(DayPage)} "
        f"WHERE lane = :lane AND persona_id = :persona_id AND date = :date "
        f"LIMIT 1"
    )
    async with get_session() as s:
        r = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id, "date": date}
        )
        return r.first() is not None


async def read_day_page_before(
    *, lane: str, persona_id: str, before_date: str
) -> DayPage | None:
    """取日期**严格早于** ``before_date`` 的最新一版昨天页，没有返回 None。

    life 与 chat 注入「她最近一页昨天」的统一读口（2026-06-12 事故修复的配套
    口径）：清晨回笼觉的快班会给**当前生活日**写下凌晨的短页，跨日取最新会把
    它错当「上一页日子」；按单字段 marker（LifeState.day_reviewed_date）取又
    会被回笼觉推前 / 对账班回拨。这里只认日期：严格早于当前生活日
    （``before_date`` 由调用方按 living_day 口径算好传入）的最新一版才是
    「昨天」。照 acts.py 的先例做 framework 没提供的只读查询；``date`` 是
    YYYY-MM-DD 文本、字典序即时间序，按 date 降序、version 降序取第一行
    （同日多版取重写后的最新一版）。
    """
    sql = (
        f"SELECT * FROM {_table_name(DayPage)} "
        f"WHERE lane = :lane AND persona_id = :persona_id AND date < :before_date "
        f"ORDER BY date DESC, version DESC LIMIT 1"
    )
    async with get_session() as s:
        r = await s.execute(
            text(sql),
            {"lane": lane, "persona_id": persona_id, "before_date": before_date},
        )
        row = r.mappings().first()
        if not row:
            return None
        return DayPage(**{k: row[k] for k in DayPage.model_fields})


async def write_relationship_page(
    *,
    lane: str,
    persona_id: str,
    other_user_id: str,
    narrative: str,
    written_at: str,
) -> None:
    """append 一版关系页（睡前回顾对这个人的「他与我」整篇重写）。

    没有删除态：淡了就在新版里自然淡，旧版靠 append-only 链留痕。durable 语义
    同 write_day_page：无 dedup、重跑无害、读侧只认最新版。
    """
    await insert_append(
        RelationshipPage(
            lane=lane,
            persona_id=persona_id,
            other_user_id=other_user_id,
            narrative=narrative,
            written_at=written_at,
        )
    )


async def read_relationship_page(
    *, lane: str, persona_id: str, other_user_id: str
) -> RelationshipPage | None:
    """读她对某人最新一版关系页，没有返回 None（第一次聊才有第一版）。"""
    return await select_latest(
        RelationshipPage,
        {"lane": lane, "persona_id": persona_id, "other_user_id": other_user_id},
    )


async def read_relationship_pages(
    *, lane: str, persona_id: str, other_user_ids: list[str]
) -> dict[str, RelationshipPage]:
    """按 user_id 列表逐页取最新关系页：有页的人 user_id → 页，没页的人缺席。

    回顾本体按「当天聊过的人」喂证据用——名单里混着第一次聊的人是常态，缺席
    不补占位（注入侧无页整段不出现，同 chat 注入策略）。逐键 ``select_latest``
    即可：一天聊过的人数量级很小，不为它造批量 SQL（业务代码不是 SDK）。
    """
    pages: dict[str, RelationshipPage] = {}
    for other_user_id in other_user_ids:
        page = await read_relationship_page(
            lane=lane, persona_id=persona_id, other_user_id=other_user_id
        )
        if page is not None:
            pages[other_user_id] = page
    return pages
