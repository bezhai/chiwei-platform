"""Durable intent 读取查询 — world 被 intent 唤醒时读全那一批意图.

为什么需要这条查询：intent→world 的 60s 合并闸（``wire(IntentWorldTick)
.debounce(...)``）是 latest-only 语义，闸到点只把**最后一条** intent 的 payload
透给 world；而上游 ``IntentRaised`` 的 durable 边已 ack。结果 1min 窗口内前面几条
intent 对 world **等价丢失**——life "想去厨房 / 出门" 的意图被静默吞掉，困死她的
能动性。EventArrived 那条 debounce 安全是因为真实 event 落在 mailbox、life 醒来
``list_unread_events`` 读全；intent 没有这个"积压可读"模型，所以这里给它补上：
world 被 intent 唤醒时从 PG 读最近一段时间所有 intent 全部呈现给 world（对称 life
读 mailbox）。

为什么不用框架 ``select_latest``：select_latest 是"每个 key 取最新一版"语义，而
这里要的是"一段时间窗里所有 intent 的列表"——是时间窗集合，不是 latest。所以在
framework 持久化写好的真实表（``data_intent_raised``，命名由 migrator 的
``data_{to_snake(ClassName)}`` 规则决定）上做一个它没提供的时间窗只读查询。写入
仍走 framework 的 ``raise_intent`` → ``emit(IntentRaised)`` → durable publish，不
绕开 framework 持久化原语。

跨时区命门：life 写的 ``occurred_at`` 是 UTC ISO（见 ``life_wake._TZ = UTC``），
world 的 ``world_time`` / since 截断是 CST。``occurred_at`` 列是 TEXT，直接字面串
比较跨偏移量会判错（``...00:30+00:00`` 字面 < ``...08:00+08:00`` 但真实时刻反过来）。
所以 SQL 里把两侧都 ``::timestamptz`` cast 成真实时刻比较，offset-aware 正确。
"""

from __future__ import annotations

from sqlalchemy import text

from app.data.session import get_session
from app.domain.world_events import IntentRaised
from app.runtime.migrator import _table_name

_INTENT_TABLE = _table_name(IntentRaised)


async def list_recent_intents(
    *, lane: str, since_iso: str
) -> list[IntentRaised]:
    """读某 lane 下 ``occurred_at >= since_iso`` 的所有 intent，按发生时间升序。

    ``since_iso`` 是真实时刻截断点（ISO8601，带偏移量）。SQL 两侧都 cast
    ``::timestamptz`` 做真实时刻比较——life 写 UTC、world 用 CST 也不会判错。
    lane 过滤保证泳道隔离（coe / ppe 的 intent 绝不被 prod 的 world 读到）。

    返回完整 ``IntentRaised`` 行（persona / summary / intent_id 都在），供 world
    把这一批所有人的意图拼进 prompt、逐条裁决。
    """
    # :since_iso 先 ::text 再 ::timestamptz —— 否则 asyncpg 见 (:since_iso)::timestamptz
    # 会把 bind param 类型推成 datetime、拒绝传入的 str（DataError）。先标成 text
    # 让 Postgres 自己解析 ISO 串（含偏移量）成真实时刻。
    #
    # occurred_at::timestamptz 对存储行做 cast：依赖唯一生产方 raise_intent（life_tools）
    # 写的永远是 offset-aware ISO（datetime.now(UTC).isoformat()）。若将来新增 IntentRaised
    # 生产方，必须同样写 offset-aware ISO，否则脏行会让整批 cast 抛错——生产侧守住这条
    # 契约，读侧不在 SQL 里吞错（吞错会静默漏 intent，与本修复初衷相悖）。
    sql = (
        f"SELECT * FROM {_INTENT_TABLE} "
        f"WHERE lane = :lane "
        f"  AND occurred_at::timestamptz >= (:since_iso)::text::timestamptz "
        f"ORDER BY occurred_at::timestamptz ASC"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "since_iso": since_iso}
        )
        rows = result.mappings().all()
        return [
            IntentRaised(**{k: row[k] for k in IntentRaised.model_fields})
            for row in rows
        ]
