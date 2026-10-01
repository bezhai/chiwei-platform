"""知识来源"别人告诉世界的事"：别的参与者发给 world 的消息，按发送方存，可以查谁最近说过什么。

**收哪些。** 别的参与者发给 world 的消息。居民说自己在哪、在做什么、对世界做了什么，都在这
里面。不收：

* world 给自己排的醒来——那是它自己写给自己的理由，不是谁告诉它的；
* "没有送达"告知——那是 world 自己的消息被通信机制退回，也不是谁告诉它的；
* 提问——提问不经过收件处理（应答 agent 只读不写），问的话也不是在告诉世界什么。

前两样的发送方都是 world 自己（通信机制把退回的告知记成原发送方发给自己），所以判据只有
一条：发送方是不是 world。

**为什么跟记录分开放。** 记录里只有主语是世界的句子（天气、地方、物件、机构、NPC）。别人说的
他们自己的事留在这里，记录不用写主语是居民的句子；以后换成"直接问居民本人"，是再写一个经
通信机制提问的来源、在启用列表里换掉这一个，记录和主 agent 的写法都不用动。

**怎么存。** 一条消息一份文件：``<发送方目录>/<消息时间>-<消息 id>.json``，在这个来源自己的
目录里（:func:`app.world.sources.private_dir`）。发送方目录名取名字的摘要：名字由通信机制校验，
可能含有不能直接做文件名的字符；原名存在文件里。文件名以时间开头，列出来就是按时间排好的。
同一条消息再来一次（投递至少一次），算出来是同一个路径、写下的是同样的内容，还是那一份。
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from pydantic import Field

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.messaging.message import Message
from app.world.agents import when
from app.world.sources import Source, private_dir
from app.world.volume import write_atomically
from app.world.wake import WORLD

NAME = "told"

# 读一个人的消息时最多交回几条，从最近的往前。
RECENT = 20


def _root() -> Path:
    return private_dir(NAME)


def _sender_dir(sender: str) -> Path:
    return _root() / hashlib.sha256(sender.encode("utf-8")).hexdigest()[:16]


def _file_name(message: Message) -> str:
    return f"{message.time.astimezone(UTC):%Y%m%dT%H%M%S%f}-{message.message_id}.json"


async def take_in(message: Message) -> None:
    """收件处理：别的参与者发来的消息存一份；同一条再来还是那一份。"""
    if message.sender == WORLD:
        return
    target = _sender_dir(message.sender) / _file_name(message)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_atomically(
        target,
        json.dumps(
            {
                "message_id": message.message_id,
                "sender": message.sender,
                "time": message.time.isoformat(),
                "body": message.body,
            },
            ensure_ascii=False,
        ),
    )


def _kept(directory: Path) -> list[Path]:
    """一个发送方存下的消息，按时间从旧到新。以点开头的是写到一半的临时文件，不算。"""
    return sorted(
        p for p in directory.iterdir() if p.suffix == ".json" and not p.name.startswith(".")
    )


def _load(path: Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding="utf-8"))


def _senders() -> list[tuple[str, int, datetime]]:
    """发过消息的每个人：名字、几条、最近一条的时间，最近发过的排在前面。"""
    root = _root()
    if not root.is_dir():
        return []
    found = []
    for directory in root.iterdir():
        kept = _kept(directory) if directory.is_dir() else []
        if kept:
            latest = _load(kept[-1])
            found.append(
                (latest["sender"], len(kept), datetime.fromisoformat(latest["time"]))
            )
    return sorted(found, key=lambda s: s[2], reverse=True)


@tool
@tool_error("列发消息的人失败")
async def list_senders() -> str:
    """列出给世界发过消息的人：名字、发过几条、最近一条是什么时候。"""
    senders = _senders()
    if not senders:
        return "还没有人给世界发过消息。"
    lines = [f"给世界发过消息的有 {len(senders)} 个："]
    lines += [f"- {name}：{count} 条，最近一条在 {when(latest)}" for name, count, latest in senders]
    return "\n".join(lines)


@tool
@tool_error("读消息失败")
async def read_messages_from(
    name: Annotated[
        str, Field(description="发消息的人的名字，跟 list_senders 列出来的一字不差")
    ],
) -> str:
    """读一个人最近发给世界的消息原文，从新到旧：他们说自己在哪、在做什么、对世界做了什么。

    这是他们自己说的，不是世界确认过的事。
    """
    directory = _sender_dir(name)
    kept = _kept(directory) if directory.is_dir() else []
    if not kept:
        others = "、".join(s[0] for s in _senders())
        known = f"给世界发过消息的有：{others}。" if others else "还没有人给世界发过消息。"
        return f"没有收到过「{name}」的消息。{known}"
    recent = [_load(p) for p in reversed(kept[-RECENT:])]
    lines = [f"{name} 最近发给世界的 {len(recent)} 条消息（共 {len(kept)} 条，从新到旧）："]
    lines += [f"- {when(datetime.fromisoformat(m['time']))}：{m['body']}" for m in recent]
    return "\n".join(lines)


SOURCE = Source(name=NAME, tools=(list_senders, read_messages_from), intake=take_in)
