"""她收到的消息：三姐妹的收件箱，收件只存储。

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
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from pydantic import field_validator

from app.living.participants import load_residents, residents
from app.living.records import _require_aware, living_lane
from app.messaging.message import Kind, Message
from app.messaging.receiving import inbox
from app.runtime.data import Data, Key
from app.runtime.persist import insert_idempotent

logger = logging.getLogger(__name__)


class ReceivedMessage(Data):
    """她收到的一条消息。

    自然键 ``(lane, persona_id, message_id)``：同一条消息投递几遍都只落一行。纯 append：
    收到过就是收到过，没有"改一条"的语义。

    ``message_time`` 是消息自带的时间（通信机制里的 ``time``：立即发送的是发出那一刻，定时
    送达的是指定的那一刻），她的输入按它排。不按到达先后排：world 的告知和姐妹直接说的话
    走两条路，到达先后不代表发生先后。
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
