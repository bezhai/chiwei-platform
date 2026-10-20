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


# ---------------------------------------------------------------------------
# residents：谁住在这儿（residents 插件提供）
# ---------------------------------------------------------------------------

# world 在通信机制里的名字。它发来的是她察觉到的事，不是谁对她说的话。life 和 world 互不 import，
# 这个名字两边各写一份（``tests/living/test_residents.py`` 把两份钉在一起）。
WORLD = "world"

# 读书收件箱的名字：她拿起一个文件时发一条消息到这里，读那一程由收件箱的处理函数做。收件方是
# 代码，不是谁的人设。写在这里是因为姐妹的显示名不许跟它撞（residents 插件检查）。
READING = "reading"


@dataclass(frozen=True)
class Residents:
    """三姐妹的 persona_id 和她们在世界里的名字，一一对应。"""

    by_persona: dict[str, str]

    def persona_of(self, name: str) -> str | None:
        """叫这个名字的是谁；不是三姐妹之一返回 ``None``。"""
        for persona_id, known in self.by_persona.items():
            if known == name:
                return persona_id
        return None

    def names_for(self, who: list[str]) -> list[str]:
        """"说给谁"里三姐妹的 id 换成她在世界里的名字，其余原样。id 和名字的对照只在这里。

        两处用它：

          * ``say`` 记下这句话时（:func:`app.living.moment._record`）。她在 ``to`` 里写了姐妹的
            id，记下来的就是姐妹的名字，发消息时按名字认出是姐妹、直接送到她那里，跟写名字
            一样。
          * 她记下的"说给谁"摆到她眼前时（:func:`app.living.happening.own_line`）。``say`` 以前
            原样记 id，那些旧记录里是 ``chinagi`` 这种；她看到它，下一次就照着它去叫人。

        不是三姐妹的（世界里的人、写错的名字、写错的 id）原样留着，那是她写下的，她想叫的是谁
        life 不猜。同一个人写了两种叫法只留一次，按出现的先后。
        """
        return list(dict.fromkeys(self.by_persona.get(w, w) for w in who))

    def sisters_in(self, names: list[str], *, speaker: str) -> list[str]:
        """``names`` 里哪几个是 ``speaker``（persona_id）的姐妹：三姐妹之一、又不是她自己。

        按出现的先后，同一个名字只留一次。不是姐妹名字的（世界里别的人、写错的名字）都不在
        里面——那是"世界里的某个人"，由 world 判断有没有这个人、听没听见。她写的姐妹 id 在
        记下时已经换成了名字（:meth:`names_for`），到这里的就是名字。
        """
        found: list[str] = []
        for name in names:
            persona_id = self.persona_of(name)
            if persona_id is not None and persona_id != speaker and name not in found:
                found.append(name)
        return found


class WhoLivesHere(Protocol):
    """谁住在这儿：三姐妹和她们在世界里的名字。名字取自人设表，开收件箱时读一次、检查一次，之后
    开收件箱、查地址、认出她对谁说话用的都是那一份。"""

    async def load(self) -> Residents:
        """从人设表读名字、逐条检查；全部合格才记下这份对照并交回，否则抛 ``RuntimeError``。"""
        ...

    def residents(self) -> Residents:
        """读好的那份对照；还没读就抛 ``RuntimeError``。"""
        ...


# ---------------------------------------------------------------------------
# loose_ends：她心里挂着没了结的事（loose_ends 插件提供）
# ---------------------------------------------------------------------------


class LooseEnds(Protocol):
    """她心里挂着、还没了结的那份清单。"""

    async def open_count(self, *, lane: str, persona_id: str) -> int:
        """她此刻还挂着几件。"""
        ...
