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
    EVENT_KIND_SPEECH,
    NPC_SOURCE_PREFIX,
    PASSIVE_EVENT_KINDS,
    EventArrived,
    EventEnvelope,
    EventRead,
)
from app.runtime.emit import emit  # module-level so tests can monkeypatch
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent

_ENVELOPE_TABLE = _table_name(EventEnvelope)
_READ_TABLE = _table_name(EventRead)


def _occurred_at_real_instant(col: str) -> str:
    """把混格式 ``occurred_at`` 列归一成真实时刻 ``timestamptz`` 的 SQL 表达式。

    信箱里 ``occurred_at`` 是 TEXT，历史脏数据混着两种格式：chat 老链路写 Unix
    毫秒（纯数字串 ``"1780..."``，以 1 开头），world/life 写 offset-aware ISO
    （``"2026-..."``，以 2 开头）。raw TEXT 排序会把 Unix 毫秒整体排在 ISO 前
    （字符串序 ``"1" < "2"``），哪怕那条 Unix 毫秒的真实时刻其实更晚——"按发生
    先后"被打乱。这个表达式按格式分支归一到真实时刻：纯数字 → ``to_timestamp``
    （毫秒 / 1000），否则 ``::timestamptz``。

    依赖生产侧契约：非数字分支的值必须是 offset-aware ISO（world/life/chat 现在
    都写带偏移量的 ISO）。这跟 ``intents.py`` 的 ``::timestamptz`` cast 同口径——
    读侧不在 SQL 里吞解析错（吞错会静默漏 event、与"按发生先后"初衷相悖），脏数据
    由生产侧守住格式契约。chat 已改写 CST aware ISO（阶段 0），新写不再产 Unix
    毫秒；纯数字分支只为向后兼容读历史脏数据存在（spec：历史不迁移、读侧兼容读）。
    """
    return (
        f"CASE WHEN {col} ~ '^[0-9]+$' "
        f"     THEN to_timestamp({col}::double precision / 1000) "
        f"     ELSE {col}::timestamptz END"
    )


async def deliver_event(
    *,
    lane: str,
    persona_id: str,
    event_id: str,
    summary: str,
    occurred_at: str,
    kind: str = EVENT_KIND_AMBIENT,
    source: str = "world",
) -> int:
    """往 ``(lane, persona_id)`` 的信箱投一条 event,新投递的**非被动** event 敲门唤醒 life。

    幂等：同一 ``(lane, persona_id, event_id)`` 重复投递只进一行（mq
    redelivery / 双投安全）。**只有新投递（真的有新东西进信箱）的非被动 event 才 emit
    ``EventArrived`` 敲门信号**——去重命中不敲门（没有新东西，免得空唤醒）；被动 kind
    （:data:`~app.domain.world_events.PASSIVE_EVENT_KINDS`）也不敲门。敲门走 debounce
    攒批，多条积压只唤醒 life 一次（wire 见 ``event_knock_key`` + Task 3 的 life-wake
    节点）。返回实际插入的行数（0=去重命中, 1=新投递）。

    **被动 kind 不敲门（通道分离的权宜修复 v2，prod 节奏失控）：** 真动静（notify
    ambient / npc_visit speech / 真人 chat external / 日程到点 reminder）走唤醒通道——
    真有人找她该立刻响应，新投递成功就 emit ``EventArrived`` 把她叫醒（哪怕在长睡里）。
    被动 kind（当前只有 world ``sense`` 投的 surroundings 周遭切片）只落信箱当**被动
    上下文**、不 emit 唤醒：world 每推演一轮就给三姐妹各投一条周遭切片，若走唤醒通道
    （设计上永远放行、不走"到点才醒"的 gate）会把自排睡着的姐妹全敲醒、自排睡眠系统性
    睡不满。她下次自己醒来（self-wake 到点）时通过 ``list_unread_events`` 自然读到最新
    周遭。

    被动语义**落在已持久化的 kind 上**（不是投递瞬间的临时参数）：上一版用一个
    ``wake`` 参数只能覆盖这条即时敲门路径、没挡住 ``renotify_unread`` 的补敲对账
    （surroundings 入信箱就是"未读"，补敲照样叫醒），修复被绕过。现在即时敲门和补敲
    对账（:func:`list_personas_with_unread`）都读同一处 ``PASSIVE_EVENT_KINDS``。

    **这是已知权宜修复、非完美方案**，二分粗、感知延迟等取舍详见
    :data:`~app.domain.world_events.PASSIVE_EVENT_KINDS` 注释 + memory
    ``project_world_sense_wake_tradeoff``。
    """
    inserted = await insert_idempotent(
        EventEnvelope(
            lane=lane,
            persona_id=persona_id,
            event_id=event_id,
            kind=kind,
            source=source,
            summary=summary,
            occurred_at=occurred_at,
        )
    )
    if inserted and kind not in PASSIVE_EVENT_KINDS:
        await emit(EventArrived(lane=lane, persona_id=persona_id))
    return inserted


async def list_unread_events(
    *, lane: str, persona_id: str
) -> list[EventEnvelope]:
    """读某 persona 本轮未读的那批 event，按发生时间升序。

    未读 = envelope 里有、read 表里没有对应 ``(lane, persona_id, event_id)``
    的那些行。lane + persona 双重过滤保证泳道隔离 + 信息差。

    排序按**真实时刻**升序（不是 raw TEXT 字符串序）：``occurred_at`` 混着 Unix
    毫秒（历史 chat）和 ISO（world/life/新 chat），字符串序会乱排，归一到真实
    时刻才是真正的"按发生先后"（见 :func:`_occurred_at_real_instant`）。
    """
    order_expr = _occurred_at_real_instant("e.occurred_at")
    sql = (
        f"SELECT e.* FROM {_ENVELOPE_TABLE} e "
        f"LEFT JOIN {_READ_TABLE} r "
        f"  ON e.lane = r.lane "
        f" AND e.persona_id = r.persona_id "
        f" AND e.event_id = r.event_id "
        f"WHERE e.lane = :lane AND e.persona_id = :persona_id "
        f"  AND r.event_id IS NULL "
        f"ORDER BY {order_expr} ASC"
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


async def list_persona_npc_speech_in_window(
    *, lane: str, persona_id: str, start_iso: str, end_iso: str
) -> list[EventEnvelope]:
    """读某 persona 在 ``[start_iso, end_iso]`` 闭区间内收到的 NPC 来访 speech event。

    睡前回顾把姐妹跟 NPC 的来往写进关系页的证据查询（NPC 层第四刀，代码层）。NPC
    互动只活在意识流 / 信箱里、不进真实聊天记录（``find_persona_spoken_chats_in_
    window`` 读的是 ``CommonMessage``），回顾抽不到——所以这里直接从信箱按窗口捞她
    收到的 NPC speech，拿到权威的 ``npc:名字`` 机读键（不靠从被剥过前缀的 transcript
    文本里猜人名）。三重过滤只留 NPC 来访（第二刀的投递形态，:func:`app.world.tools.
    npc_visit`）：

      * ``kind = 'speech'`` —— 直接冲她来的具名话语（排除 world 环境动静
        ambient / surroundings）；
      * ``source LIKE 'npc:%'`` —— NPC 来访（排除姐妹直投的 ``source=persona_id``、
        真人外部消息 ``source=user:xxx``）。NPC 前缀约定见
        :data:`app.world.npc_roster.NPC_SOURCE_PREFIX`。

    **不看 read 表**（与 :func:`list_unread_events` 的关键区别）：回顾在睡前跑，当天的
    NPC speech 多半已被 life 标已读，按未读捞会漏掉今天的互动。窗口语义就是「这一天她
    收到过哪些 NPC 来访」，已读未读都算。

    窗口两端按真实时刻比较（``occurred_at`` 历史混 Unix 毫秒 / ISO，:func:`_occurred_
    at_real_instant` 归一），照 :func:`app.data.queries.acts.list_persona_acts_between`
    的先例在 framework 持久化写好的真实表上做只读查询（framework 没提供窗口读），按真实
    时刻升序（与意识流证据时间序对齐）；不绕开 framework 持久化原语。lane + persona
    双过滤：只读她自己的、泳道隔离。
    """
    occurred_real = _occurred_at_real_instant("e.occurred_at")
    sql = (
        f"SELECT e.* FROM {_ENVELOPE_TABLE} e "
        f"WHERE e.lane = :lane AND e.persona_id = :persona_id "
        f"  AND e.kind = :speech_kind "
        f"  AND e.source LIKE :npc_like "
        f"  AND {occurred_real} >= (:start_iso)::text::timestamptz "
        f"  AND {occurred_real} <= (:end_iso)::text::timestamptz "
        f"ORDER BY {occurred_real} ASC"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql),
            {
                "lane": lane,
                "persona_id": persona_id,
                "speech_kind": EVENT_KIND_SPEECH,
                "npc_like": f"{NPC_SOURCE_PREFIX}%",
                "start_iso": start_iso,
                "end_iso": end_iso,
            },
        )
        rows = result.mappings().all()
        return [
            EventEnvelope(**{k: row[k] for k in EventEnvelope.model_fields})
            for row in rows
        ]


async def list_personas_with_unread(*, lane: str) -> list[str]:
    """该 lane 下有**非被动**未读 event 的 distinct persona_id —— 补敲对账的读侧。

    复用 :func:`list_unread_events` 同一条 LEFT JOIN 反连接（envelope 有、read
    表无对应行 = 未读），聚到 persona 维度：``SELECT DISTINCT e.persona_id``。
    给唤醒自愈回路（:func:`renotify_unread`）用——查出"信箱里有真动静、却没人来读"
    的人，挨个补敲。

    **排除被动 kind**（:data:`~app.domain.world_events.PASSIVE_EVENT_KINDS`，通道分离
    权宜修复 v2）：world 每轮 tick 在到点 gate 之前调本查询补敲，纯 surroundings 未读
    的 persona 若被算进来，每轮 tick 的补敲就把自排睡着的姐妹叫醒（即时敲门改按 kind
    跳过被动后仍被这条补敲路径绕过的真 bug）。所以这里只算"有**非被动**未读"的人：纯
    被动未读不补敲、不打断长睡；有真动静（哪怕同时混着 surroundings）的照常补敲。

    与 :func:`list_unread_events`（她**自己醒来**读未读用的）的关键区别：那条**绝不**
    排除 surroundings——她醒来时仍要读到全部未读含周遭切片。只有**这条补敲对账**排除
    被动。两条是分开的查询，别改错。
    """
    # 被动 kind 占位：frozenset 无序，sort 后派生确定性命名占位（``:passive_0`` …）。
    # 当前只有 surroundings 一项；多于一项时下面 NOT IN 自动覆盖全部。
    passive_kinds = sorted(PASSIVE_EVENT_KINDS)
    passive_params = {f"passive_{i}": k for i, k in enumerate(passive_kinds)}
    passive_placeholders = ", ".join(f":{name}" for name in passive_params)
    sql = (
        f"SELECT DISTINCT e.persona_id FROM {_ENVELOPE_TABLE} e "
        f"LEFT JOIN {_READ_TABLE} r "
        f"  ON e.lane = r.lane "
        f" AND e.persona_id = r.persona_id "
        f" AND e.event_id = r.event_id "
        f"WHERE e.lane = :lane "
        f"  AND r.event_id IS NULL "
        f"  AND e.kind NOT IN ({passive_placeholders}) "
        f"ORDER BY e.persona_id ASC"
    )
    async with get_session() as s:
        result = await s.execute(text(sql), {"lane": lane, **passive_params})
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
