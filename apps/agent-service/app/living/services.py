"""她这边插件之间的契约：服务的接口（``Protocol``）和接口里出现的数据类型。

她的每个功能是一个插件，一个目录 ``app/living/<插件>/``。插件之间不互相 import
（``tests/living/test_plugin_boundaries.py`` 守着）：一个插件要另一个插件的东西，就由提供方在
setup 里 ``ctx.provide(名字, 对象)`` 交出服务，使用方在 :class:`~app.host.Plugin` 的 ``requires``
里写上名字、setup 里 ``ctx.service(名字)`` 取到，作为构造参数传给自己的插件对象。测试直接传替身。

这里只放签名和数据类型，不放实现；不读库、不调模型的共用纯函数在 :mod:`app.living.common`。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

# ---------------------------------------------------------------------------
# persona：「她是谁」（persona 插件提供）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PersonaText:
    """「她是谁」那条版本链上的一版，交给别的插件时的样子（不是那张表本身）。"""

    narrative: str  # 身份正文全文
    source: str  # 这版从哪来：seed / review / owner
    written_at: str  # 写下这版的现实时刻（CST ISO8601 字符串）
    version: int


class Persona(Protocol):
    """「她是谁」：喂进各条 prompt 的人设变量，和她每周改写的那条版本链。

    三种取法各答一个问题，同名变量的值不同是刻意的：

      * :meth:`prompt_vars` —— 她**现在**是谁（链上最新一版，退回主表 ``persona_core``，再退回一句
        实话）。她的一轮和写日记用。
      * :meth:`review_vars` —— 她**本来**是谁（主表 ``persona_core`` 原文，空白就抛）。每周回看用：
        参照物必须站在她改写自己的循环外面。
      * :meth:`lite_vars` —— 读书用的那份（链上最新一版，退回主表 ``persona_lite``；进程内缓存 5 分钟）。
    """

    async def prompt_vars(self, *, lane: str, persona_id: str) -> dict[str, str]:
        """``{persona_name, persona_core}``。"""
        ...

    async def review_vars(self, *, persona_id: str) -> dict[str, str]:
        """``{persona_name, persona_core}``，``persona_core`` 是主表原文；没有或空白抛 ``ValueError``。"""
        ...

    async def lite_vars(self, *, persona_id: str) -> dict[str, str]:
        """``{persona_name, persona_lite}``。"""
        ...

    async def latest(self, *, lane: str, persona_id: str) -> PersonaText | None:
        """链上最新一版，不分来源；链空返回 ``None``。"""
        ...

    async def has_review_this_week(
        self, *, lane: str, persona_id: str, now: datetime | None = None
    ) -> bool:
        """``now`` 所在的自然周（周一 00:00 CST 起）有没有 ``source='review'`` 的版本。"""
        ...

    async def seed_chain(self, *, lane: str, persona_id: str) -> bool:
        """链空时把主表 ``persona_core`` 落为第一版（``source='seed'``）；返回是否真的写了。"""
        ...

    async def write_version(
        self, *, lane: str, persona_id: str, narrative: str, source: str, written_at: str
    ) -> None:
        """append 一版身份正文。"""
        ...
