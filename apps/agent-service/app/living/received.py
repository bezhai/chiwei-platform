"""她收到的消息：三姐妹的收件箱，收件只存储；她下一轮醒来时读。

**收件箱开在 agent-service 进程里，名字是她们在世界里的名字**（:mod:`app.living.participants`）。
名字存在人设表里，接线模块 import 的时候库还没准备好，所以接线只声明"开始接收时再开"
（:func:`app.messaging.receiving.inboxes_at_start`），:func:`open_inboxes` 在那时读名字、检查、
逐个开设。名字有问题就抛，进程起不来。

**收件处理只做存储，不调模型**（:func:`receive`）。没选"收到就当场跑她一轮"：她每一轮已经
有自己的锁和节奏（:mod:`app.living.moment`），收件处理只负责把消息存好，不碰她的循环；
失败了也只是通信机制再投一次、再写一次。守这条的是 ``tests/living/test_no_inbound.py``。

**存下来和去重是同一条语句**（``INSERT ... ON CONFLICT DO NOTHING``，自然键
``(lane, persona_id, message_id)``），不会存了一半。通信机制的投递是至少一次，world 还会带着
原来的 id 重发没发完的告知：同一条到两遍只存一行。存之前出事，这次处理失败，通信机制再投；
存下之后、确认之前出事，再投的那一次撞上自然键，什么都不多写。

存的是消息原样：谁发的、正文、消息自带的时间。没有地点、没有渠道——通信机制的外层只有这几
样，其余都写在正文里。到达的时刻是框架那一列 ``created_at``。

``not_delivered`` 告知不存：那是她自己定时发出的消息被退回，而她从不定时发消息；真来了也
不是她经历的事，只留一条日志。

**她下一轮醒来时读**（:func:`unread_received` → :func:`render_received`，由
:func:`app.living.moment.run_moment` 摆进这一轮的输入）。按每条消息自带的时间排，不按到达
先后。收件箱里一条消息把她提前叫醒的那一轮，那一条一定在里面（``including``）。world 发来的是她察觉到的事，原样摆，不标是谁说的；别人发来的带着发送方的名字。

**读到哪里逐条记，不是一个水位**（:class:`ReceivedRead`）。按消息自带的时间开水位会漏：
姐妹直接说的话和 world 的告知走两条路，一条早发生的可能晚到，水位已经越过它的时间，它就再也
摆不到她眼前。按到达先后开水位也不行：这一轮跑着的时候新到的那条会排在水位之前还是之后，取决于
谁先落库。所以每一条记下"它放进过哪一轮"，没记的就是没看过，跟顺序无关。

**只记这一轮实际放进输入的那几条，跟这一轮的 ``LifeMoment`` 在同一个事务里落地**
（:func:`mark_read`）。这一轮跑着的时候新到的不在这几条里，留到下一轮；这一轮失败了一条都
不记，下一轮原样再给她看。跟手机已读同一个道理（:func:`app.living.phone.commit_glances`）：
宁可重看，不可漏看。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any

from pydantic import field_validator
from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import dated_clock
from app.living.participants import WORLD, load_residents, residents
from app.living.records import _require_aware, esc, living_lane
from app.messaging.message import Kind, Message
from app.messaging.receiving import inbox
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent

logger = logging.getLogger(__name__)

# 一轮最多摆多少条。不是截断：没摆进来的留着没看，下一轮接着拿最早的那几条。60 条约等于半小时
# 的动静：服务停过一阵、world 攒下一批告知时，过几轮就追平，不会一轮塞进几百条。
RECEIVED_LIMIT = 60

# 她这一轮输入里这一段的开头。空的时候如实说空，不留白洞。
_HEAD = "这段时间传到你这里的："


class ReceivedMessage(Data):
    """她收到的一条消息。

    自然键 ``(lane, persona_id, message_id)``：同一条消息投递几遍都只落一行。纯 append：
    收到过就是收到过，没有"改一条"的语义。

    ``message_time`` 是消息自带的时间（通信机制里的 ``time``：发送方给的、它所说的事发生的那一刻，
    没给就是发出那一刻；定时送达的是指定的那一刻），她的输入按它排。不按到达先后排：world 的告知
    和姐妹直接说的话走两条路，补发的消息也会晚到，到达先后不代表发生先后。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    message_id: Annotated[str, Key]
    sender: str
    body: str
    message_time: datetime

    class Meta:
        # 读侧唯一形状：这个人收到的消息，按消息自带的时间排。
        indexes = (("lane", "persona_id", "message_time"),)

    @field_validator("message_time")
    @classmethod
    def _aware_message_time(cls, v: datetime) -> datetime:
        return _require_aware("message_time", v)


class ReceivedRead(Data):
    """她看过某一条收到的消息：它放进了哪一轮，那一轮落了地。

    自然键 ``(lane, persona_id, message_id)``：一条消息只算看过一次。``moment_id`` 是哪一轮
    （可查，不进键）。纯 append：这条记的是"她看过这一条"这件发生过的事，不是一个会被改写的
    水位，理由见模块说明。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    message_id: Annotated[str, Key]
    moment_id: str

    class Meta:
        # 读侧唯一形状：这个人的某一条看过没有（:func:`unread_received` 的 NOT EXISTS）。
        indexes = (("lane", "persona_id", "message_id"),)


_RECEIVED_TABLE = _table_name(ReceivedMessage)
_READ_TABLE = _table_name(ReceivedRead)


async def open_inboxes() -> None:
    """读三姐妹的名字，按名字各开一个收件箱。通信机制开始接收之前调一次。"""
    known = await load_residents()
    for name in known.by_persona.values():
        inbox(name, on_message=receive)


async def receive(message: Message) -> None:
    """三姐妹收件箱的处理函数：把这条存到收件人名下。只存，不调模型。"""
    if message.kind is not Kind.MESSAGE:
        logger.warning(
            "living: %s 收到一条 %s（%s），不存：她从不定时发消息，这不是她经历的事。原文：%s",
            message.recipient,
            message.kind,
            message.message_id,
            message.body,
        )
        return
    persona_id = residents().persona_of(message.recipient)
    if persona_id is None:
        raise RuntimeError(
            f"收件人 {message.recipient!r} 不是三姐妹之一，可她的收件箱是按同一份对照开的"
        )
    await insert_idempotent(
        ReceivedMessage(
            lane=living_lane(),
            persona_id=persona_id,
            message_id=message.message_id,
            sender=message.sender,
            body=message.body,
            message_time=message.time,
        )
    )


async def unread_received(
    *,
    lane: str,
    persona_id: str,
    limit: int = RECEIVED_LIMIT,
    including: str | None = None,
) -> list[ReceivedMessage]:
    """她收到、还没看过的消息里最早的 ``limit`` 条，按消息自带的时间排。

    ``including`` 是叫醒她这一轮的那条消息的 id（:mod:`app.living.nudge`）：它还没看过而又
    不在最早那几条里时，也摆进来，这一轮就多出这一条。那一轮的身份就是它，那一轮落地它就该
    算看过；只取最早那几条的话，叫醒她之后才到、发生得更早的消息一多，就会把它挤出去。

    同一刻的几条按消息 id 排，只是为了每次读出来的顺序一样。
    """
    unread = (
        f"SELECT m.* FROM {_RECEIVED_TABLE} m "
        f"WHERE m.lane = :lane AND m.persona_id = :persona_id "
        f"AND NOT EXISTS (SELECT 1 FROM {_READ_TABLE} r "
        f"WHERE r.lane = m.lane AND r.persona_id = m.persona_id "
        f"AND r.message_id = m.message_id)"
    )
    sql = (
        f"({unread} ORDER BY m.message_time, m.message_id LIMIT :limit) "
        f"UNION ({unread} AND m.message_id = :including) "
        f"ORDER BY message_time, message_id"
    )
    params = {
        "lane": lane,
        "persona_id": persona_id,
        "limit": limit,
        "including": including,
    }
    async with get_session() as s:
        rows = (await s.execute(text(sql), params)).mappings().all()
    return [
        ReceivedMessage(**{k: row[k] for k in ReceivedMessage.model_fields})
        for row in rows
    ]


async def read_received_between(
    *, lane: str, persona_id: str, since: datetime, until: datetime
) -> list[ReceivedMessage]:
    """她收到的消息里，说的是 ``[since, until)`` 之间的事的那些，按消息自带的时间排。

    日记那一页（:mod:`app.living.day_page`）读的就是这个。按消息自带的时间开窗，不按到达先后：
    一条说九点半的事、十点才到的告知，属于九点半所在的那一天。看没看过都算：日记是在那一天过完
    之后才写的，传到她这里的就是她那一天的一部分，跟哪一轮先摆给她看无关。

    同一刻的几条按消息 id 排，只是为了每次读出来的顺序一样（同 :func:`unread_received`）。
    """
    sql = (
        f"SELECT * FROM {_RECEIVED_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"AND message_time >= :since AND message_time < :until "
        f"ORDER BY message_time, message_id"
    )
    params = {"lane": lane, "persona_id": persona_id, "since": since, "until": until}
    async with get_session() as s:
        rows = (await s.execute(text(sql), params)).mappings().all()
    return [
        ReceivedMessage(**{k: row[k] for k in ReceivedMessage.model_fields})
        for row in rows
    ]


def render_received(items: list[ReceivedMessage], *, now: datetime) -> str:
    """这一轮摆到她眼前的那一段：每条带着它自己的时刻。"""
    if not items:
        return f"{_HEAD}（没有）"
    lines = [
        f"- {dated_clock(m.message_time, now=now)} {received_line(m)}" for m in items
    ]
    return _HEAD + "\n" + "\n".join(lines)


def received_line(item: ReceivedMessage) -> str:
    """收到的一条在她眼里的样子。她每一轮读到的、日记材料里摆的，都是这一个样子。

    world 发来的是她察觉到的事（窗外下雨了、有人敲门），原样摆，不标是谁说的——那不是谁对她
    说的话。别人发来的带着发送方的名字。

    发送方和正文都过 :func:`app.living.records.esc`：正文由发送方写下，world 和姐妹那边是
    模型、人工参与者那边是人，哪一种都不归她管，而这一列上转义没有代价。她自己的经历不过
    （:func:`app.living.happening.own_line`），判据写在 esc 上。
    """
    if item.sender == WORLD:
        return esc(item.body)
    return f"{esc(item.sender)}：{esc(item.body)}"


async def mark_read(
    items: list[ReceivedMessage], *, moment_id: str, session: Any
) -> None:
    """把这一轮放进她输入的那几条记成看过。**由这一轮的收尾调用，跟 ``LifeMoment`` 同一个事务。**"""
    for item in items:
        await insert_idempotent(
            ReceivedRead(
                lane=item.lane,
                persona_id=item.persona_id,
                message_id=item.message_id,
                moment_id=moment_id,
            ),
            session=session,
        )
