"""world 的知识来源：world 的 agent 能查到的东西，都由这里登记的来源提供。

一个来源（:class:`Source`）有三样：

* **名字**：启用列表里写的就是它（:data:`ENABLED_SOURCES_KEY`），它的插件也叫这个名字。
* **一组只读的查询工具**：工具描述写明它知道什么。模型从工具描述知道去哪查，所以 prompt 不
  列举来源，加一个来源也不用改 prompt。查询工具的返回都是读到的材料（:attr:`Source.query_tools`，
  只在这一处判断）。
* **可选的收件处理**（``intake``）：world 收件箱每收到一条普通消息（别人发来的、自己排的
  醒来、"没有送达"告知；提问不算，提问只读不写），先交给每个启用的来源（:func:`take_in`），
  来源只把自己需要的部分存进自己的私有目录（:func:`private_dir`）。投递至少一次，同一条
  消息可能来两次，所以收件处理按消息 id 去重：同一条再来，什么都不变。

**每个来源是一个插件**（:func:`source_plugin`）：起来时把来源登记进这里的登记表，把查询工具登记给
宿主（``ctx.tool``，清单 ``world`` 排它们给主 agent 的位置）；停下时把来源从登记表里拿掉。

**四个 agent 拿到的只读工具是同一份来源**：清单把查询工具的名字列一次（
:data:`app.manifests.world.QUERY_TOOLS`），主 agent 的工具和感知判断、NPC、应答的 AgentSpec 都用它；
关掉的来源由开关拿掉（一轮里按这一轮开始时读的，见 :func:`app.agent.round.run_round`；应答按每个
问题来时读的）。主 agent 另有它自己才有的几个动作（:mod:`app.world.actions`），不在这里。

**加一个来源**：在这个包里新增它的模块，定义一个 :class:`Source` 和它的插件
（``PLUGIN = source_plugin(SOURCE)``），在 world 的清单（:mod:`app.manifests.world`）里列上插件，
把它的工具名加进 ``QUERY_TOOLS``，加进开关能关掉的插件。不改任何 agent，也不改 prompt。

**启用哪些走 Dynamic Config**（:data:`ENABLED_SOURCES_KEY`，逗号分隔的名字）。没配就是登记过
的全部；配了就只启用列出来的，没登记过的名字记一条 warning、跳过。没启用的来源，工具从四个
agent 里同时消失，收件处理也不跑。
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from inner_shared.dynamic_config import dynamic_config

from app.agent.contracts import ToolSpec
from app.agent.tooling import Tool
from app.host import Context, Plugin
from app.messaging.message import Message
from app.world.volume import lane_dir

logger = logging.getLogger(__name__)

# Dynamic Config：启用哪些来源，逗号分隔的名字。没配就是登记过的全部。改它不用重新部署。
ENABLED_SOURCES_KEY = "world_sources"

Intake = Callable[[Message], Awaitable[None]]


@dataclass(frozen=True)
class Source:
    """一个知识来源：名字、只读的查询工具、可选的收件处理。"""

    name: str
    tools: tuple[Tool, ...]
    intake: Intake | None = None

    @property
    def query_tools(self) -> tuple[ToolSpec, ...]:
        """它的查询工具，按工具声明交给 agent：返回的都是读到的材料（``trim="material"``，
        :mod:`app.agent.continuity` 按它分两档保留）。"""
        return tuple(ToolSpec(t, trim="material") for t in self.tools)


_registered: dict[str, Source] = {}


def register(source: Source) -> None:
    """登记一个来源。名字或者工具名跟已经登记的重了就抛 ``ValueError``：模型按工具名调，
    两个同名的工具只有一个够得着。"""
    if source.name in _registered:
        raise ValueError(f"knowledge source {source.name!r} is already registered")
    taken = {t.name for s in _registered.values() for t in s.tools}
    clash = taken & {t.name for t in source.tools}
    if clash:
        raise ValueError(
            f"knowledge source {source.name!r} reuses tool names {sorted(clash)}"
        )
    _registered[source.name] = source


def unregister(source: Source) -> None:
    """把 ``source`` 从登记表里拿掉；登记着的不是它（没登记上，或者同名的是另一个）就什么都不做。"""
    if _registered.get(source.name) is source:
        del _registered[source.name]


def clear_sources() -> None:
    """清空登记表：一个新进程起来时它是空的（回放的进程隔离用它还原）。"""
    _registered.clear()


def source_plugin(source: Source) -> Plugin:
    """``source`` 的插件，名字就是来源的名字：起来时把来源登记进登记表、把查询工具登记给宿主，
    停下时从登记表里拿掉它。"""

    def setup(ctx: Context) -> None:
        # 先登记撤销再登记来源：setup 中途失败时，宿主停下也会把已经登记的来源拿掉。
        ctx.on_stop(lambda: unregister(source))
        register(source)
        for spec in source.query_tools:
            ctx.tool(spec.tool, trim=spec.trim)

    return Plugin(name=source.name, setup=setup)


async def enabled_sources() -> list[Source]:
    """现在启用的来源，按登记的先后。"""
    raw = await asyncio.to_thread(dynamic_config.get, ENABLED_SOURCES_KEY, default="")
    if not raw.strip():
        return list(_registered.values())
    wanted = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = wanted - _registered.keys()
    if unknown:
        logger.warning(
            "dynamic config %s names unregistered sources %s; skipped",
            ENABLED_SOURCES_KEY,
            sorted(unknown),
        )
    return [s for name, s in _registered.items() if name in wanted]


async def take_in(message: Message) -> None:
    """把收件箱刚收到的一条交给每个启用的、有收件处理的来源。失败原样往外抛。"""
    for source in await enabled_sources():
        if source.intake is not None:
            await source.intake(message)


def private_dir(name: str) -> Path:
    """来源 ``name`` 在私有卷上的目录（不一定已经存在）：``$WORLD_DATA_DIR/<泳道>/sources/<名字>/``。

    跟 world 的记录（``records/``）分开放，记录的人工读写接口够不到这里。
    """
    return lane_dir() / "sources" / name
