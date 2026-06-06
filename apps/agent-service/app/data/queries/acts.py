"""Durable act 读取查询 — world 醒来按复合游标批量 pull act.

pull 范式：act 不再唤醒 world。life 做完一件事直接 ``insert_idempotent`` 落
``data_act_performed`` 表，world 按自己 sleep 的节奏醒来时从"上次消费游标之后"批量
读这段时间攒下的 act 一并推演，推完把游标推进到本批末尾。这条查询是读侧底座：
按复合游标 ``(created_at, act_id)`` 过滤、限量读最早 N 条（剩下下轮接着读）。

为什么游标用 ``created_at`` 而不是 ``occurred_at``：``occurred_at`` 是 life 在轮次
开始就固定的"做事时刻"，act 工具稍后才落库——跨 persona 并发时 occurred_at 顺序
≠ 落库顺序。若按 occurred_at 推进游标，会先消费"晚发生但早落库"的 act 把游标推过
去，之后落库的"早发生" act 永远读不到（漏 act、违反"act 不丢"）。``created_at`` 是
framework 给每个 Data 表自动加的 ``TIMESTAMPTZ DEFAULT now()``（见 migrator）、
**单调的落库时刻**——按它推进游标只会前进、不会跳过任何尚未落库的行，不漏。

为什么是复合游标而不是只用 created_at：同一落库瞬间理论上可能有多条 act。只用
created_at 的 ``>`` 会漏掉边界同刻的新行、``>=`` 会重读边界旧行。加 ``act_id`` 作
稳定 tie-breaker（act_id 只需字典序确定即可，不要求时间序），读取条件是
``created_at > 游标 OR (created_at = 游标 AND act_id > 游标act_id)``。游标为 None
（冷启动、从没消费过）时退化成"读全既有 act"。

为什么不用框架 ``select_latest``：select_latest 是"每个 key 取最新一版"语义，而
这里要的是"游标之后一段窗里所有 act 的列表"——是游标集合，不是 latest。所以在
framework 持久化写好的真实表（``data_act_performed``，命名由 migrator 的
``data_{to_snake(ClassName)}`` 规则决定）上做一个它没提供的复合游标只读查询。写入
仍走 framework 的 ``perform_act`` → ``insert_idempotent(ActPerformed)``，不绕开
framework 持久化原语。

返回值带 created_at：``created_at`` 不在 ``ActPerformed.model_fields`` 里（是
runtime 列），但 engine 收口推进游标、起点比对都要它——所以从 SELECT 行单独取出、
与 ``ActPerformed`` 一并返回 ``list[tuple[ActPerformed, str]]``。其中 str 是这行
``created_at`` 归一成 ISO 文本（``::timestamptz::text``，offset-aware）。
"""

from __future__ import annotations

from sqlalchemy import text

from app.data.session import get_session
from app.domain.world_events import ActPerformed
from app.runtime.migrator import _table_name

_ACT_TABLE = _table_name(ActPerformed)


async def list_recent_acts(
    *,
    lane: str,
    cursor_created_at: str | None,
    cursor_act_id: str | None,
    limit: int,
) -> list[tuple[ActPerformed, str]]:
    """读某 lane 下复合游标 ``(cursor_created_at, cursor_act_id)`` 之后的最早 ``limit`` 条 act。

    游标语义：``created_at > 游标 OR (created_at = 游标 AND act_id > 游标act_id)``。
    游标任一为 None（冷启动 / 从没消费过）时不加游标过滤、读全既有。按 ``(created_at,
    act_id)`` 落库时刻 + 字典序升序，取最早的 ``limit`` 条（积压超 limit 的剩下下轮从
    本批末尾接着读、不丢）。

    游标用 ``created_at``（单调落库序）而非 ``occurred_at``（life 轮首固定的做事时刻、
    与落库顺序可乱序）——是 out-of-order 漏读的命门：见模块 docstring。

    返回 ``list[tuple[ActPerformed, str]]``：每条 act 行 + 它的 ``created_at`` ISO
    文本。``created_at`` 不在 ``ActPerformed.model_fields`` 里（runtime 列），单独
    从 SELECT 行取出归一成 ISO（``::timestamptz::text`` offset-aware），供 engine
    算游标终点 / 起点。act_id 是 act 的稳定标识，普通字符串字典序比即可。lane 过滤
    保证泳道隔离（coe / ppe 的 act 绝不被 prod 的 world 读到）。
    """
    params: dict[str, object] = {"lane": lane, "limit": limit}
    cursor_clause = ""
    if cursor_created_at is not None and cursor_act_id is not None:
        # 复合游标过滤：游标落库时刻之后，或同刻且 act_id 字典序更大。
        # :cursor_created_at 先 ::text 再 ::timestamptz —— 否则 asyncpg 见
        # (:p)::timestamptz 会把 bind param 类型推成 datetime、拒绝传入的 str
        # （DataError）。先标成 text 让 Postgres 自己解析 ISO 串（含偏移量）成真实时刻。
        cursor_clause = (
            "  AND ("
            "created_at > (:cursor_created_at)::text::timestamptz "
            "OR (created_at = (:cursor_created_at)::text::timestamptz "
            "AND act_id > :cursor_act_id)"
            ") "
        )
        params["cursor_created_at"] = cursor_created_at
        params["cursor_act_id"] = cursor_act_id

    # created_at 是 framework 自动加的 TIMESTAMPTZ DEFAULT now() 列（单调落库序）。
    # SELECT 时把它归一成 ISO 文本（::text，offset-aware）返回，让 engine 拿去当下次
    # 的游标值（同一列同一口径，回填游标比对不会有格式漂移）。
    sql = (
        f"SELECT *, created_at::text AS _created_at_iso FROM {_ACT_TABLE} "
        f"WHERE lane = :lane "
        f"{cursor_clause}"
        f"ORDER BY created_at ASC, act_id ASC "
        f"LIMIT :limit"
    )
    async with get_session() as s:
        result = await s.execute(text(sql), params)
        rows = result.mappings().all()
        return [
            (
                ActPerformed(**{k: row[k] for k in ActPerformed.model_fields}),
                row["_created_at_iso"],
            )
            for row in rows
        ]
