"""将要发生什么 —— 客观日历项的写入与到期交付。

日出日落、三餐、店关门、快递到：这些是数据，写下来就行，不花模型钱。

**交付条件是"到期了且还没被拿走过"，不是一段时间窗。** 时间窗
（``(after, until]``）只在"所有项必定提前写入"这个假设下才对，而这个假设从来没被
编码过：重启补种、工具重试、world 晚一步提交一条已经过了游标的 item，全都会被永久
越过。所以这里让消费方拿走之后自己标一笔（:func:`mark_upcoming_consumed`），没标
掉的下一缝还会再来——崩在半路是"重来一次"，不是"丢一条"。

代价说清楚：**至少一次，不是恰好一次。** 拿到手了但没来得及标就崩，下一缝会再拿到
同一条。消费侧要用 item_id 派生的幂等键写 :class:`~app.living.records.Happening`，
重放才是无害的。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text

from app.data.session import get_session
from app.living.records import Upcoming
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_append, select_latest

_TABLE = _table_name(Upcoming)


async def schedule_upcoming(
    *,
    lane: str,
    item_id: str,
    what: str,
    due_at: datetime,
    place: str | None = None,
) -> bool:
    """写下一件将要发生的事；返回是不是**这次调用**写下的。

    幂等靠 CAS：只在这个 item 还一版都没有的时候写 v1。重放（甚至消费之后再重放）
    都不会覆盖已有的版本链——不然重投一次就把"已经拿走过"抹掉了，同一件事会被她
    感知两遍。

    返回值跟 :func:`mark_upcoming_consumed` 同款语义（"这一笔是我记的吗"）：调用方
    每一拍都重排一遍日历时，靠它分得清"这次真的新写了几条"和"全是重放"——没有它，
    重排就只能靠再查一次库或者干脆报不出来。
    """
    return 1 == await insert_append(
        Upcoming(
            lane=lane,
            item_id=item_id,
            ver=0,  # 由 insert_append 按 expected_current_ver 赋成 1
            what=what,
            due_at=due_at,
            place=place,
        ),
        expected_current_ver=0,
    )


async def list_due_upcoming(
    *, lane: str, until: datetime
) -> list[Upcoming]:
    """到 ``until`` 为止已经到期、且还没被消费掉的日历项，按到期先后升序。

    每个 item 只看版本链上最新的一版：``consumed_at`` 一旦被填上，这条就不再出现。
    右端点是闭的（``<= until``）——正好到点的那一刻算到期。
    """
    sql = (
        f"SELECT * FROM ("
        f"  SELECT DISTINCT ON (lane, item_id) * FROM {_TABLE} "
        f"  WHERE lane = :lane ORDER BY lane, item_id, ver DESC"
        f") latest "
        f"WHERE consumed_at IS NULL AND due_at <= :until "
        f"ORDER BY due_at ASC, item_id ASC"
    )
    async with get_session() as s:
        result = await s.execute(text(sql), {"lane": lane, "until": until})
        rows = result.mappings().all()
    return [Upcoming(**{k: row[k] for k in Upcoming.model_fields}) for row in rows]


async def list_upcoming_between(
    *, lane: str, since: datetime, until: datetime
) -> list[Upcoming]:
    """账上 ``due_at`` 落在 ``[since, until]`` 里的每一件事，**不管消费过没有**。

    跟 :func:`list_due_upcoming` 是两个问题，别混：那个问"现在该交付什么"（所以
    只给没被拿走的），这个问"这段时间的账长什么样"——**已经发生过的必须在里面**。
    读它的是要往账上添新东西的人（world 的稀疏轮次）：看不见刚发生过的快递，它下
    一轮就会再排一次快递；看不见还没到的晚饭，它会再排一次晚饭。

    两端都是闭的，按到期先后升序。每个 item 只看版本链上最新的一版。
    """
    sql = (
        f"SELECT * FROM ("
        f"  SELECT DISTINCT ON (lane, item_id) * FROM {_TABLE} "
        f"  WHERE lane = :lane ORDER BY lane, item_id, ver DESC"
        f") latest "
        f"WHERE due_at >= :since AND due_at <= :until "
        f"ORDER BY due_at ASC, item_id ASC"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "since": since, "until": until}
        )
        rows = result.mappings().all()
    return [Upcoming(**{k: row[k] for k in Upcoming.model_fields}) for row in rows]


async def mark_upcoming_consumed(
    *, lane: str, item_id: str, consumed_at: datetime
) -> bool:
    """把这条日历项标成已经拿走；返回是不是**这次调用**标掉的。

    已经标过就返回 ``False`` 且不改写原来的消费时刻——重投 / 重试会走到这里。
    并发下两个人同时标，CAS 让其中一个拿到 ``False``。

    从没写下过的 item_id 直接 :class:`LookupError`：那是调用方凭空造了个 id，
    静默吞掉只会让"日历项没到期"变成一个查不出来的现象。
    """
    latest = await select_latest(Upcoming, {"lane": lane, "item_id": item_id})
    if latest is None:
        raise LookupError(
            f"upcoming {item_id!r} 在 lane {lane!r} 上从来没被写下过 —— "
            f"消费一个不存在的日历项，多半是 item_id 拼错了"
        )
    assert isinstance(latest, Upcoming)
    if latest.consumed_at is not None:
        return False
    # 走构造函数而不是 ``model_copy(update=...)``：后者在 pydantic v2 上完全跳过
    # 校验，naive 的 ``consumed_at`` 会从这条缝里溜进 TIMESTAMPTZ 列。
    written = await insert_append(
        Upcoming(**{**latest.model_dump(), "consumed_at": consumed_at}),
        expected_current_ver=latest.ver,
    )
    return written == 1
