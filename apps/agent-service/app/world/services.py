"""world 这边插件之间的契约：一个插件给别的插件用的东西，接口写在这里（Protocol），连同接口里
出现的数据类型。

提供它的插件在 setup 里交出（``ctx.provide``，名字列在插件的 ``provides`` 里）；用它的插件在
``requires`` 里列上名字、setup 里取到（``ctx.service``），作为构造参数交给自己的插件对象，测试时
直接换成替身。world 的插件目录之间不互相 import，只 import 这里、共享模块和框架公开的东西
（CI 规则 ``scripts/check_plugin_boundaries.py``）。

* ``world_intake``（world 核心提供，told 和 answer 用）：:class:`WorldIntake`。
* ``perception``（perception 提供，npc 用）：:class:`Perception`。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from app.host import Disposer
from app.messaging.message import Message

# 一个插件的收件处理：world 收件箱刚收到的一条普通消息。
Intake = Callable[[Message], Awaitable[None]]
# 回答问 world 的一个问题：交回回答的话，什么都没说就是 ``None``。
Answer = Callable[[Message], Awaitable["str | None"]]


class WorldIntake(Protocol):
    """world 收件箱交给别的插件的两样：收件处理——每收到一条普通消息（别人发来的、自己排的
    醒来、"没有送达"告知；提问不算，提问只读不写），交给登记过的、这一刻没被开关关掉的插件，按
    登记的先后；提问——交给登记的那一个应答。"""

    def register(self, plugin: str, intake: Intake) -> Disposer:
        """登记插件 ``plugin`` 的收件处理，交回取消登记的 disposer。

        投递至少一次，同一条消息可能来两次，所以 ``intake`` 按消息 id 去重：同一条再来，什么都
        不变。它失败原样往外抛，这次投递按处理失败重试。
        """
        ...

    def answer_with(self, plugin: str, answer: Answer) -> Disposer:
        """问 world 的问题交给插件 ``plugin`` 的 ``answer`` 回答，交回取消登记的 disposer。最多一个
        插件登记，第二个抛 ``ValueError``；没有登记时收件箱对提问什么都不说。

        提问只读：它不经过收件处理，也不叫醒主 agent。``answer`` 失败原样往外抛，通信机制告诉
        提问方"没有回答"。
        """
        ...


@dataclass(frozen=True)
class Notice:
    """一条判断出来的告知：发给谁、发什么、要不要叫醒他、用哪个消息 id。

    id 在判断完那一刻定下，之后不管发几次都用它，接收方按它去重。要不要叫醒也在那一刻定下，
    补发时原样带着。
    """

    who: str
    what: str
    wakes_recipient: bool
    message_id: str


class Perception(Protocol):
    """一个变化谁会察觉、各自察觉到什么，以及告知他们。"""

    async def judge_who_notices(
        self, change: str, *, round_messages: Sequence[Message]
    ) -> list[Notice]:
        """为 ``change`` 跑一次感知判断，交回判断出来的告知，每条带一个新的消息 id；一条都不发。
        ``round_messages`` 是主 agent 这一轮处理的消息，按到达的先后。失败原样往外抛。"""
        ...

    async def tell(self, what: str, notices: Sequence[Notice]) -> str:
        """先把这件事（``what``，给主 agent 看的那段话）连同要发的告知记下来，再按各自的消息 id
        发出去，交回给主 agent 看的结果：告知了谁、告知的什么、送没送达。

        记下来之后的失败（写不进卷、发送出错、被取消）原样往外抛：这一轮按失败重来，下一轮开始
        时按原 id 补发。
        """
        ...
