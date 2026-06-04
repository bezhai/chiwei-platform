"""Durable event 信箱读写 — Task 1 (event 流转骨架).

三个对外动作，构成 world/对话 → life 的投递面：

  * :func:`deliver_event` —— 往某 persona 的信箱投一条 event（幂等去重）。
  * :func:`list_unread_events` —— 读某 persona 本轮未读的那批 event。
  * :func:`mark_events_read` —— 标已读，**只标传入的那批 event_id**。

为什么读未读不用框架 ``select_latest``：framework 的 as_latest / select_latest
是"每个 key 取最新一版"语义，而信箱要的是"这个 persona 名下所有还没读的
event"——是集合差，不是 latest。所以这里用 framework 持久化写入的真实表
（``data_event_envelope`` / ``data_event_read``，命名由 migrator 的
``data_{to_snake(ClassName)}`` 规则决定）做一条 LEFT JOIN 反连接的只读查询。
写入仍走 framework 的 ``insert_idempotent``（durable 去重）——不绕开 framework
的持久化原语，只在它写好的表上做一个它没提供的集合差查询。

mark_events_read 按 event_id 逐条插 EventRead，绝不按 persona 全标——life 想
一轮期间新进的 event 没有 read 行，下一轮仍是未读，不会被静默吞掉。
"""

from __future__ import annotations

from sqlalchemy import text

from app.data.session import get_session
from app.domain.world_events import (
    EVENT_KIND_AMBIENT,
    EventArrived,
    EventEnvelope,
    EventRead,
)
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent

_ENVELOPE_TABLE = _table_name(EventEnvelope)
_READ_TABLE = _table_name(EventRead)


async def deliver_event(
    *,
    lane: str,
    persona_id: str,
    event_id: str,
    summary: str,
    occurred_at: str,
    kind: str = EVENT_KIND_AMBIENT,
    source: str = "world",
    room_id: str = "",
) -> int:
    """往 ``(lane, persona_id)`` 的信箱投一条 event,投成功后敲门唤醒 life。

    幂等：同一 ``(lane, persona_id, event_id)`` 重复投递只进一行（mq
    redelivery / 双投安全）。**只有新投递（真的有新东西进信箱）才 emit
    ``EventArrived`` 敲门信号**——去重命中不敲门,免得空唤醒。敲门走 debounce
    攒批，多条积压只唤醒 life 一次（wire 见 ``event_knock_key`` + Task 3 的
    life-wake 节点）。返回实际插入的行数（0=去重命中, 1=新投递）。
    """
    inserted = await insert_idempotent(
        EventEnvelope(
            lane=lane,
            persona_id=persona_id,
            event_id=event_id,
            kind=kind,
            source=source,
            room_id=room_id,
            summary=summary,
            occurred_at=occurred_at,
        )
    )
    if inserted:
        await emit(EventArrived(lane=lane, persona_id=persona_id))
    return inserted


async def list_unread_events(
    *, lane: str, persona_id: str
) -> list[EventEnvelope]:
    """读某 persona 本轮未读的那批 event，按发生时间升序。

    未读 = envelope 里有、read 表里没有对应 ``(lane, persona_id, event_id)``
    的那些行。lane + persona 双重过滤保证泳道隔离 + 信息差。
    """
    sql = (
        f"SELECT e.* FROM {_ENVELOPE_TABLE} e "
        f"LEFT JOIN {_READ_TABLE} r "
        f"  ON e.lane = r.lane "
        f" AND e.persona_id = r.persona_id "
        f" AND e.event_id = r.event_id "
        f"WHERE e.lane = :lane AND e.persona_id = :persona_id "
        f"  AND r.event_id IS NULL "
        f"ORDER BY e.occurred_at ASC"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id}
        )
        rows = result.mappings().all()
        return [
            EventEnvelope(**{k: row[k] for k in EventEnvelope.model_fields})
            for row in rows
        ]


async def list_personas_with_unread(*, lane: str) -> list[str]:
    """该 lane 下还有未读 event 的 distinct persona_id —— 信箱对账的读侧。

    复用 :func:`list_unread_events` 同一条 LEFT JOIN 反连接（envelope 有、read
    表无对应行 = 未读），只是聚到 persona 维度：``SELECT DISTINCT e.persona_id``。
    给唤醒自愈回路用——查出"信箱里有东西、却没人来读"的人，挨个补敲。
    """
    sql = (
        f"SELECT DISTINCT e.persona_id FROM {_ENVELOPE_TABLE} e "
        f"LEFT JOIN {_READ_TABLE} r "
        f"  ON e.lane = r.lane "
        f" AND e.persona_id = r.persona_id "
        f" AND e.event_id = r.event_id "
        f"WHERE e.lane = :lane "
        f"  AND r.event_id IS NULL "
        f"ORDER BY e.persona_id ASC"
    )
    async with get_session() as s:
        result = await s.execute(text(sql), {"lane": lane})
        return [row["persona_id"] for row in result.mappings().all()]


async def renotify_unread(*, lane: str) -> int:
    """信箱对账自愈：对该 lane 下每个还有未读的 persona 补敲一次 ``EventArrived``。

    deliver_event 的"落库 + 敲门"两步非原子——落库成功但 emit 敲门撞上瞬时
    redis 失败时，event 会永久躺在信箱里没人读。这个对账函数不依赖那次敲门是否
    成功：查出真有未读的 persona、挨个重发 ``EventArrived`` 让 life-wake 有机会被
    重新唤醒。补敲幂等安全——life_wake_node 一进来 ``list_unread_events``，空信箱
    直接 early-return，且有 single_flight 锁，所以补敲没未读 / 正在思考的 persona
    都无害。由 world 的保底心跳（纯 in-process、不依赖 redis）每轮调一次，丢掉的
    敲门最多一个心跳周期后补上。返回补敲了几个 persona。
    """
    personas = await list_personas_with_unread(lane=lane)
    for persona_id in personas:
        await emit(EventArrived(lane=lane, persona_id=persona_id))
    return len(personas)


async def mark_events_read(
    *, lane: str, persona_id: str, event_ids: list[str]
) -> None:
    """标已读：**只标传入的那批 event_id**。

    为每个 event_id 插一行 EventRead（幂等）。绝不按 persona 全标——本轮没读
    到的 event（含想一轮期间新进的）不在 event_ids 里，不会被标，下一轮仍未读。
    """
    for event_id in event_ids:
        await insert_idempotent(
            EventRead(lane=lane, persona_id=persona_id, event_id=event_id)
        )
