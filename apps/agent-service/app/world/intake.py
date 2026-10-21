"""world 收件箱的 ``world_intake`` 服务（:class:`~app.world.services.WorldIntake`）：收件箱交给别的
插件的收件处理和提问。world 核心提供它（:mod:`app.plugins.world`），用它的插件在 setup 里登记。

**收件处理**（:meth:`Intakes.take_in`，收件箱的处理函数 :meth:`app.world.rounds.Rounds.receive`
先调它）：每收到一条普通消息，交给登记过的插件的收件处理，按登记的先后；被开关关掉的插件的跳过。
开关是 world 一轮的那一份（清单 ``world`` 的 ``switches``，Dynamic Config ``world_sources``），收件处理
不在哪一轮里，每条消息来时读一次（:func:`app.agent.round.switched_off`）；开关不管的插件的收件处理
总在。哪一个失败原样往外抛，后面的不再跑，这次投递按处理失败重试（收件处理都按消息 id 去重）。

**提问**（:meth:`Intakes.answer`，收件箱的 ``on_question``）：交给登记的那一个应答（最多一个），在
提问来时找它；没有登记时什么都不说（``None``），跟应答什么都没说一样。world 核心不依赖应答插件：
功能插件依赖核心，核心不反过来依赖它们。
"""
from __future__ import annotations

from app.agent.contracts import Switches
from app.agent.round import switched_off
from app.host import Disposer
from app.messaging.message import Message
from app.world.services import Answer, Intake


class Intakes:
    """一个 world 进程的 ``world_intake``：宿主每次起来，world 插件新建一个（跟 :class:`app.world.rounds.Rounds` 一样，
    不放在模块级）。``switches`` 是 world 一轮的开关。"""

    def __init__(self, switches: Switches | None) -> None:
        self._switches = switches
        self._intakes: list[tuple[str, Intake]] = []
        self._answer: tuple[str, Answer] | None = None

    def register(self, plugin: str, intake: Intake) -> Disposer:
        entry = (plugin, intake)
        self._intakes.append(entry)

        async def dispose() -> None:
            self._intakes[:] = [e for e in self._intakes if e is not entry]

        return dispose

    def answer_with(self, plugin: str, answer: Answer) -> Disposer:
        if self._answer is not None:
            raise ValueError(
                f"world's questions are already answered by plugin {self._answer[0]!r}; "
                f"plugin {plugin!r} cannot answer them too"
            )
        entry = (plugin, answer)
        self._answer = entry

        async def dispose() -> None:
            if self._answer is entry:
                self._answer = None

        return dispose

    async def take_in(self, message: Message) -> None:
        """把一条普通消息交给每个登记了、没被关掉的插件的收件处理，按登记的先后。"""
        off = await switched_off(self._switches)
        for plugin, intake in list(self._intakes):
            if plugin not in off:
                await intake(message)

    async def answer(self, question: Message) -> str | None:
        """回答一个提问：交给登记的应答；没有登记就是 ``None``。"""
        if self._answer is None:
            return None
        return await self._answer[1](question)
