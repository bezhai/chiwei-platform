"""Jotting — 赤尾的随笔（随手记：当下的念头、观察、感想）+ 吸收水位窗口.

本子（NotebookEntry）拆分出来的另一条通道：``note`` 的机制含义是"常驻输入、等待
了结的待办"，而她"记录当下"的冲动（观察流水账、突然的感想）没有可完成性、不该
常驻——硬塞进本子就是 872 行膨胀 + done 不涨的根因（spec 决策 1）。随笔是**纯
append 的草稿纸**：无三态 status、无 remind_at、无编辑、无版本链——写了就写了。

生命周期到当天日结为止（spec 决策 2「工具可见窗口，不物理删」）：

  * **显式吸收水位**：``JottingWatermark`` 独立版本链存"已吸收到哪"，不从 day
    page 的存在性 / 时间隐式推导。翻页动作只在日页**落笔成功之后**由 review 收口
    处调（那是调用方语义）；本模块保证翻页动作本身幂等、水位单调不回退。
  * **复合游标 ``(created_at, jot_id)``**：窗口读只取水位之后的行。游标用
    framework 自动写的 ``created_at``（单调落库时刻）而非 ``noted_at``（她轮首的
    主观时刻、与落库顺序可乱序——按它推进会把"晚落库的早时刻"随笔永远翻过去，
    漏证据；同 acts.py pull 游标的命门）。``jot_id`` 作同刻 tie-breaker：只用
    created_at 的 ``>`` 漏边界同刻新行、``>=`` 重读旧行。
  * **游标文本定宽可比**：窗口读把 ``created_at`` 归一成 UTC 定宽 ISO
    （``to_char(... AT TIME ZONE 'UTC', '....US"+00:00"')``，微秒恒 6 位）——
    定宽 + 同一时区 → 字典序即时间序，翻页的单调守卫用纯字符串元组比较即可，
    不需要解析时刻（pg 默认 ``::text`` 的小数位数可变、字典序会撒谎，所以不用它）。
  * **review 中途新写的随笔不丢**：翻页只翻到"读窗口那一刻"的游标，之后落库的
    随笔天然在水位之后、留给下一次日结。review 失败 / 重试期间不调翻页 → 水位
    不动、随笔仍在窗口内。多日未回顾的累积随笔一次全部入窗（不按自然日切分）。

Key 带 lane（泳道隔离命门）：runtime 持久化不会自动加 lane，不显式带上 coe / ppe
泳道就会写脏 / 翻掉 prod 的草稿纸——同其它 durable Data（NotebookEntry / LifeState）。
``noted_at`` 而非 ``created_at``：``created_at`` 是 runtime 保留列（migrator 自动加
的落库时刻），不能做业务字段名（同 LifeState.observed_at 的教训）。

「记一条」幂等（durable mutation，对称 ``note_entry`` / ``perform_act``）：
``insert_idempotent`` 按 ``(lane, persona_id, jot_id)`` 去重（无 Version、ver 不折进
hash）——整轮重试 / durable 重投用同一派生 jot_id 再写一次 ON CONFLICT DO NOTHING、
只落一条。``jot_id`` 由触发源派生（工具层负责），不让模型生成。
"""

from __future__ import annotations

from typing import Annotated, NamedTuple

from sqlalchemy import text

from app.data.session import get_session
from app.runtime.data import Data, Key, Version
from app.runtime.migrator import _table_name
from app.runtime.persist import (
    insert_append,
    insert_idempotent,
    select_latest,
)


class Jotting(Data):
    """草稿纸上的一条随笔：一段大白话 + 她写下的时刻。纯 append、无版本链。

    自然键 ``(lane, persona_id, jot_id)``：泳道隔离 + 每人一张草稿纸 + 每条一行。
    没有 status / remind_at ——随笔没有"了结"，只有"被日页吸收后翻过去"。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    jot_id: Annotated[str, Key]
    content: str    # 她随手写的一段大白话（念头 / 观察 / 感想）
    noted_at: str   # 她写下这条的现实时刻 (ISO8601)

    class Meta:
        # 窗口读 / 计数的支撑索引：两条查询都按 (lane, persona_id) 过滤 + 复合
        # 游标 (created_at, jot_id) 增量取——count 每个 life 轮都调（stimulus
        # 存在提示行），而随笔全历史只增不删，没有索引会随历史线性退化（违反
        # spec「窗口读 / 计数不扫全历史」）。列序即查询形态：键列在前、游标列
        # 在后。migrator 对已存在的表也补建（IF NOT EXISTS 幂等）。
        indexes = (("lane", "persona_id", "created_at", "jot_id"),)


class JottingWatermark(Data):
    """随笔吸收水位：她的草稿纸"已被日页吸收到哪"。as_latest（带 Version）。

    自然键 ``(lane, persona_id)``：每人每泳道一条水位链。每次翻页 append 一版
    （水位推进历史全留痕、可观测，不 UPSERT 覆盖），对外只认最新一版。字段是
    复合游标坐标 ``(absorbed_created_at, absorbed_jot_id)`` —— 定宽 UTC ISO 文本
    （由窗口读的 ``to_char`` 归一产出，字典序即时间序）+ 同刻 tie-breaker。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    absorbed_created_at: str  # 已吸收到的落库时刻（定宽 UTC ISO 文本，字典序可比）
    absorbed_jot_id: str      # 同刻 tie-breaker（该行的 jot_id）
    turned_at: str            # 这次翻页的现实时刻 (ISO8601)，排查留痕


class JottingCursor(NamedTuple):
    """窗口读给出的翻页游标：窗口末行的 ``(created_at 定宽文本, jot_id)`` 坐标。

    NamedTuple 的元组比较就是单调守卫的比较口径（created_at 定宽文本字典序即
    时间序，同刻再比 jot_id）。调用方原样拿去 :func:`turn_jotting_page`，不解释
    也不自造。
    """

    created_at: str
    jot_id: str


class JottingWindow(NamedTuple):
    """一次窗口读的结果：窗口内随笔（落库序）+ 翻页游标（空窗口为 None）。"""

    jottings: list[Jotting]
    cursor: JottingCursor | None


# 把 framework 的 created_at（TIMESTAMPTZ）归一成**定宽** UTC ISO 文本：微秒恒
# 6 位（``US``）、恒 ``+00:00`` 后缀 → 同格式字符串字典序即时间序，单调守卫可以
# 纯字符串比较。不用 ``::text``（pg 默认渲染小数位可变长，".1" 与 ".05" 字典序
# 会撒谎）。单一定义处：窗口读产游标、翻页守卫比游标，两边同一口径。
_CURSOR_TEXT_SQL = (
    "to_char(created_at AT TIME ZONE 'UTC', "
    "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"+00:00\"')"
)


def _cursor_filter(watermark: JottingWatermark | None) -> tuple[str, dict]:
    """水位 → 窗口读 / 计数共用的 SQL 过滤片段 + 绑定参数（单一定义处）。

    复合游标语义：``created_at > 水位 OR (created_at = 水位 AND jot_id > 水位jot_id)``。
    没有水位（从没翻过页、冷启动）→ 不过滤、全部随笔都未吸收。绑定参数先
    ``::text`` 再 ``::timestamptz``：asyncpg 会把 ``(:p)::timestamptz`` 的 bind
    类型推成 datetime、拒绝 str（同 acts.py 的坑），先标 text 让 pg 自己解析。
    """
    if watermark is None:
        return "", {}
    clause = (
        " AND (created_at > (:wm_created_at)::text::timestamptz "
        "OR (created_at = (:wm_created_at)::text::timestamptz "
        "AND jot_id > :wm_jot_id))"
    )
    return clause, {
        "wm_created_at": watermark.absorbed_created_at,
        "wm_jot_id": watermark.absorbed_jot_id,
    }


async def _latest_watermark(
    *, lane: str, persona_id: str
) -> JottingWatermark | None:
    """读她当前的吸收水位（最新一版），从没翻过页返回 ``None``。"""
    return await select_latest(
        JottingWatermark, {"lane": lane, "persona_id": persona_id}
    )


async def jot_down(
    *, lane: str, persona_id: str, jot_id: str, content: str, noted_at: str
) -> None:
    """记一条随笔 → ``insert_idempotent`` 落一行 Jotting。

    durable 幂等：整轮重试 / durable 重投用同一 ``(lane, persona_id, jot_id)``
    再写一次 → dedup 按键去重、ON CONFLICT DO NOTHING、只落一条。随笔没有编辑 /
    状态，落了就是落了——不做任何内容校验（她想怎么写就怎么写，机制不设闸）。
    """
    await insert_idempotent(
        Jotting(
            lane=lane,
            persona_id=persona_id,
            jot_id=jot_id,
            content=content,
            noted_at=noted_at,
        )
    )


async def read_unabsorbed_jottings(
    *, lane: str, persona_id: str
) -> JottingWindow:
    """窗口读：取她还没被日页吸收的全部随笔（落库序）+ 本次窗口的翻页游标。

    只取水位之后的增量（SQL 过滤，不扫全历史再在 Python 里筛）；多日未回顾的
    累积随笔一次全部入窗、不按自然日切分（spec 决策 2）。游标是窗口末行的
    ``(created_at 定宽文本, jot_id)``——review 日页落笔成功后原样传给
    :func:`turn_jotting_page`；窗口读本身**不动水位**（life 翻随笔工具随便读，
    吸收只发生在日结翻页）。空窗口游标为 ``None``（无可翻）。
    """
    watermark = await _latest_watermark(lane=lane, persona_id=persona_id)
    clause, params = _cursor_filter(watermark)
    sql = (
        f"SELECT *, {_CURSOR_TEXT_SQL} AS _cursor_created_at "
        f"FROM {_table_name(Jotting)} "
        f"WHERE lane = :lane AND persona_id = :persona_id"
        f"{clause} "
        f"ORDER BY created_at ASC, jot_id ASC"
    )
    async with get_session() as s:
        r = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id, **params}
        )
        rows = r.mappings().all()
    jottings = [
        Jotting(**{k: row[k] for k in Jotting.model_fields}) for row in rows
    ]
    cursor = (
        JottingCursor(
            created_at=rows[-1]["_cursor_created_at"],
            jot_id=rows[-1]["jot_id"],
        )
        if rows
        else None
    )
    return JottingWindow(jottings=jottings, cursor=cursor)


async def count_unabsorbed_jottings(*, lane: str, persona_id: str) -> int:
    """窗口内随笔条数（life stimulus 一行存在提示用）。

    与 :func:`read_unabsorbed_jottings` 同一份水位判据（``_cursor_filter``
    单一定义处），但只 COUNT、不取正文——存在提示每轮都拼，不为一个数字捞全文。
    """
    watermark = await _latest_watermark(lane=lane, persona_id=persona_id)
    clause, params = _cursor_filter(watermark)
    sql = (
        f"SELECT COUNT(*) FROM {_table_name(Jotting)} "
        f"WHERE lane = :lane AND persona_id = :persona_id"
        f"{clause}"
    )
    async with get_session() as s:
        r = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id, **params}
        )
        return int(r.scalar() or 0)


async def turn_jotting_page(
    *,
    lane: str,
    persona_id: str,
    cursor: JottingCursor | None,
    turned_at: str,
) -> None:
    """窗口翻页：把吸收水位推进到 ``cursor``（幂等、单调不回退）。

    只在 review 日页**落笔成功之后**由收口处调（调用方语义；失败 / 重试期间不调
    → 水位不动、随笔不丢）。三条机制保证：

      * **``cursor=None`` no-op**：空窗口本就无可翻，直接返回（不落行、不抛）。
      * **单调守卫**：游标 ``<=`` 当前水位（NamedTuple 元组比较，定宽文本字典序
        即时间序）→ no-op。重复翻同一游标、迟到的旧游标重试都被挡住，不 append
        冗余版本、水位绝不回退（回退会让已吸收随笔复活、次日重复吸收）。
      * **CAS 收口**：``insert_append(expected_current_ver=...)`` 把"没人在我读
        之后又推过水位"折进 INSERT 原子判——并发翻页（sweep 补跑撞上正班）也不会
        在新水位之上叠一版旧水位；CAS 输了就重读重判，直到确认 no-op 或写成。
    """
    if cursor is None:
        return
    while True:
        watermark = await _latest_watermark(lane=lane, persona_id=persona_id)
        if watermark is not None and cursor <= JottingCursor(
            created_at=watermark.absorbed_created_at,
            jot_id=watermark.absorbed_jot_id,
        ):
            return
        written = await insert_append(
            JottingWatermark(
                lane=lane,
                persona_id=persona_id,
                absorbed_created_at=cursor.created_at,
                absorbed_jot_id=cursor.jot_id,
                turned_at=turned_at,
            ),
            expected_current_ver=watermark.ver if watermark is not None else 0,
        )
        if written:
            return


def render_jottings(jottings: list[Jotting]) -> str:
    """把窗口内随笔渲成给模型看的文字（每条一行：内容 + 她写下的时刻）。

    **单一定义处**（宪法「禁止重复定义」）：life 翻随笔工具、review「今天的随笔」
    证据段共用这一份渲染。随笔没有 id / 状态 / 提醒时间可渲——不可编辑、不可了结，
    行里只有内容和时刻。空窗口给一句提示（不返回空串让模型困惑）。
    """
    if not jottings:
        return "草稿纸上没有未翻页的随笔。"
    return "\n".join(f"- {j.content}（记于 {j.noted_at}）" for j in jottings)
