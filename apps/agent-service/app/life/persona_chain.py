"""persona 版本链 — 「她是谁」的身份正文落点（persona 周级慢漂的地基）.

三姐妹的身份正文原本只活在 ``bot_persona.persona_lite``（写死的出厂快照，零代码
写路径）。周级 persona review 不 UPDATE 主表，而是落这条 framework Data 版本链
（照 WorldArc / DayPage 模板，第六次复用）：Key + narrative + written_at +
Version、append-only、读最新一版、整篇重写——新版**取代**旧版。主表保留不动，
作 v0 来源和冷启 fallback。

每版带来源 ``source``，一条链同时承担三件事、不拆来源就互相污染（spec 决策 2）：

  * ``seed``  —— 出厂灌入：链为空时把 ``bot_persona.persona_lite`` 原文落为第一版
    （:func:`seed_persona_chain`，幂等、重跑无害）。
  * ``review`` —— 自动慢漂：周级 review agent 写的版本。**自动班的幂等
    （:func:`has_review_version_this_week`）与证据游标
    （:func:`read_latest_review_written_at`）都只认它**。
  * ``owner`` —— bezhai 干预：人工盖版。读路径（:func:`read_latest_persona_version`
    不分来源）即刻生效，但既不挡当周自动班、也不推走证据窗口。

周界 = **自然周一 00:00 CST**（:func:`week_start_cst`）。生活日是 04:00 界，但
persona 慢漂是周级的钟，周界用自然周一零点（spec 决策 4），两个口径不混。

写入走 framework 的 ``insert_append``（Version 自增），读最新走 ``select_latest``；
按来源过滤是 framework 没提供的只读查询，照 acts.py / day_page_exists 的先例在
framework 持久化写好的真实表上直接 SELECT——不绕开 framework 持久化原语。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated

from app.data.queries import find_latest_persona_review_written_at, find_persona
from app.infra.cst_time import CST, now_cst, now_cst_iso
from app.infra.cst_time import parse as parse_time
from app.runtime.data import Data, Key, Version
from app.runtime.persist import insert_append, select_latest


class PersonaVersion(Data):
    """「她是谁」身份正文的自然语言全文快照（一版）.

    自然键 ``(lane, persona_id)``：泳道隔离（coe / ppe 绝不能覆盖 prod 的
    persona）+ 每个角色一条链。``narrative`` 是整篇重写的身份正文全文——与
    ``bot_persona.persona_lite`` 同族口吻，注入方零适配。``source`` 是这一版
    从哪来（seed / review / owner，见模块 docstring——读 b / 读 c 只认 review）。
    ``written_at`` 是写下这版的现实时刻（CST ISO8601）——命名避开框架保留列
    （``created_at`` 是框架的落库时刻，语义不同且是保留列，同 DayPage 教训）。
    ``version`` 让多版正文 append-only 保留历史、读最新一版。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    narrative: str    # 身份正文全文（整篇重写的自然语言）
    source: str       # 这版从哪来：seed（出厂灌入）/ review（自动慢漂）/ owner（bezhai 干预）
    written_at: str   # 写下这版的现实时刻 (CST ISO8601)
    version: Annotated[int, Version] = 0


async def write_persona_version(
    *, lane: str, persona_id: str, narrative: str, source: str, written_at: str
) -> None:
    """append 一版身份正文（带来源）。

    durable 语义同 write_day_page：append 新版本、无 dedup。整次 review 失败重跑
    可能再 append 一次语义相同的版本——无害，读侧只认最新版，版本链留痕。
    """
    await insert_append(
        PersonaVersion(
            lane=lane,
            persona_id=persona_id,
            narrative=narrative,
            source=source,
            written_at=written_at,
        )
    )


async def read_latest_persona_version(
    *, lane: str, persona_id: str
) -> PersonaVersion | None:
    """读 a：最新一版身份正文，**不分来源**——owner 盖版即生效；没有返回 None
    （冷启：读侧 fallback ``bot_persona`` 主表）。"""
    return await select_latest(
        PersonaVersion, {"lane": lane, "persona_id": persona_id}
    )


async def read_latest_review_written_at(
    *, lane: str, persona_id: str
) -> str | None:
    """读 c：最新一条 **source='review'** 版本的 written_at；没有返回 None。

    review 的证据游标——下一班只消化这个时点之后写下的页。owner / seed 版本
    绝不入选：bezhai 人工盖版不能把证据窗口推走（spec 决策 2）。首跑（链上
    还没有 review 版）返回 None = 窗口取全部现存页。
    """
    return await find_latest_persona_review_written_at(
        PersonaVersion,
        lane=lane,
        persona_id=persona_id,
    )


def week_start_cst(now: datetime) -> datetime:
    """``now`` 所在自然周的周一 00:00 CST（aware datetime）。

    persona 慢漂的周界口径（spec 决策 4）：生活日是 04:00 界，但周级的钟用
    **自然周一零点**——别的时区的 aware 时刻先归一到 CST 再取周界。
    """
    local = now.astimezone(CST)
    monday = local.date() - timedelta(days=local.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=CST)


async def has_review_version_this_week(
    *, lane: str, persona_id: str, now: datetime | None = None
) -> bool:
    """读 b：本周（周一 00:00 CST 起）是否已有 **source='review'** 的版本。

    自动班的幂等口径：True = 本周班已完成、今天不跑。只认 review——同周的
    owner / seed 版本在场仍返回 False（bezhai 盖版不挡自动班，spec 决策 2）。
    written_at 解析失败（理论上不会：全部由 now_cst_iso 写出）按"不在本周"算
    ——宁可多跑一班，不能让脏数据把慢漂永远卡死（fail-open 方向一致）。
    """
    latest = await read_latest_review_written_at(
        lane=lane, persona_id=persona_id
    )
    if latest is None:
        return False
    written = parse_time(latest)
    if written is None:
        return False
    return written >= week_start_cst(now if now is not None else now_cst())


async def seed_persona_chain(*, lane: str, persona_id: str) -> bool:
    """v0 灌入：链为空时把 ``bot_persona.persona_lite`` 原文落为第一版
    （source='seed'）；链非空零操作。返回是否真的写入了。

    幂等靠 ``insert_append`` 的 CAS（``expected_current_ver=0``：只有链上
    MAX(version)=0 即一版都没有时才插入），检查和写入是同一条原子语句——
    重跑无害、并发双跑也只落一行。``bot_persona`` 没这行 = 没有原文可灌，
    fail fast 不静默写空版。
    """
    persona = await find_persona(persona_id)
    if persona is None:
        raise ValueError(
            f"seed_persona_chain: bot_persona has no row for "
            f"persona_id={persona_id!r} — nothing to seed from"
        )
    inserted = await insert_append(
        PersonaVersion(
            lane=lane,
            persona_id=persona_id,
            narrative=persona.persona_lite,
            source="seed",
            written_at=now_cst_iso(),
        ),
        expected_current_ver=0,
    )
    return inserted == 1
