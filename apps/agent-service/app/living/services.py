"""她这边插件之间的契约：服务的接口（``Protocol``）和接口里出现的数据类型。

她的每个功能是一个插件，一个目录 ``app/living/<插件>/``。插件之间不互相 import
（CI 规则 ``scripts/check_plugin_boundaries.py`` 守着）：一个插件要另一个插件的东西，就由提供方在
setup 里 ``ctx.provide(名字, 对象)`` 交出服务，使用方在 :class:`~app.host.Plugin` 的 ``requires``
里写上名字、setup 里 ``ctx.service(名字)`` 取到，作为构造参数传给自己的插件对象。测试直接传替身。

这里只放签名和数据类型，不放实现；不读库、不调模型的共用纯函数在 :mod:`app.living.common`。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.agent.round import Round

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

          * ``say`` 记下这句话时（:func:`app.living.life.moment._record`）。她在 ``to`` 里写了姐妹的
            id，记下来的就是姐妹的名字，发消息时按名字认出是姐妹、直接送到她那里，跟写名字
            一样。
          * 她记下的"说给谁"摆到她眼前时（:func:`app.living.her_records.deeds.own_line`）。``say`` 以前
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
# her_records：她自己的经历和她在哪（her_records 插件提供）
# ---------------------------------------------------------------------------

# 一件经历的形态。机制层硬定的两类，不是让模型自由发挥的字符串：
#   * ``speech``  说出口的话。``content`` 是原话。
#   * ``act``     做的事。``content`` 是一句自然语言描述（"我去厨房煮抹茶"）。
# 两者共用一张表，是因为「她对谁做了什么说了什么」在读取侧是同一件事：她回看自己的经历、
# 一轮结束时把当面做的事按先后汇总给 world。
KIND_SPEECH = "speech"
KIND_ACT = "act"

# 她通过嘴发出去的一条消息，在她经历上的 id 前缀：``mouth:<outbound_id>``。前缀之后那一串就是
# 撤回要用的键（``SpokenOutbound.outbound_id``）。
#
# **只许有这一处定义。** 拼它的是发消息那只手，把它剥掉、好让她在发送回执、快照和日记材料里
# 看见那个编号的是 :func:`app.living.common.message_handle`，而她照抄那个编号去撤回时按等值查
# 的就是同一个键。两边各写一份字面量的话，改了一处就漂移，而漂移的表现是她照抄了却撤不掉
# —— 一句报错都没有。
OUTBOUND_HAPPENING_PREFIX = "mouth:"

# 通过什么渠道。这是**客观事实**——她是当面说的，还是拿手机发的，还是发在群里的。
# 不是给她的行为分优先级，也不是强度分级：三个值之间没有高低，只有"声音能不能传到
# 旁边的人耳朵里"这一条物理差别。
#   * ``in_person``   当面说 / 当场做。进 world 的汇总，谁会察觉由 world 判断。
#   * ``phone``       私聊消息。隔着设备，旁边的人看不见，只在她自己的记录里。
#   * ``group_chat``  群里说话。同上。
MEDIUM_IN_PERSON = "in_person"
MEDIUM_PHONE = "phone"
MEDIUM_GROUP_CHAT = "group_chat"


class Deed(Protocol):
    """她的一件经历（做过的事、说过的话），交给别的插件时读得到的样子。``seq`` 是本泳道的提交序。"""

    happening_id: str
    seq: int
    actor: str
    kind: str
    medium: str
    content: str
    occurred_at: datetime
    audience: list[str]
    channel_id: str | None


class Place(Protocol):
    """她在哪、在做什么的一条，交给别的插件时读得到的样子。``seq`` 是她自己那条轴上的号。"""

    moment_id: str
    seq: int
    place: str
    doing: str
    noted_at: datetime


@dataclass(frozen=True)
class SinceMarks:
    """她两个号之后的经历和位置（往外讲她做了什么用），各带落库时刻，按号的先后。"""

    deeds: list[tuple[datetime, Deed]]
    moves: list[tuple[datetime, Place]]
    before: Place | None  # 这一段之前她最后在哪；没有是 ``None``


class HerRecords(Protocol):
    """她自己的经历和她在哪：只记她自己的，也只给她自己读（每个方法都按一个人筛）。"""

    async def record_happening(
        self,
        *,
        lane: str,
        happening_id: str,
        actor: str,
        kind: str,
        content: str,
        occurred_at: datetime,
        audience: Sequence[str] = (),
        medium: str = MEDIUM_IN_PERSON,
        channel_id: str | None = None,
    ) -> Deed:
        """记下她做过的一件事，并记进这一轮做了几件（必须在她的一轮里调）。同一个 id 只落一行。"""
        ...

    def recorded_this_round(self, round: Round) -> int:
        """这一轮她真的落下了几件事（去重后）。"""
        ...

    async def note_whereabouts(
        self,
        *,
        lane: str,
        persona_id: str,
        moment_id: str,
        place: str,
        doing: str,
        noted_at: datetime,
    ) -> Place:
        """记下她这一轮在哪、在做什么。同一 ``moment_id`` 只落一行。"""
        ...

    async def current_whereabouts(self, *, lane: str, persona_id: str) -> Place | None:
        """她当前在哪、在做什么；从没记过返回 ``None``。"""
        ...

    async def happenings_between(
        self, *, lane: str, persona_id: str, since: datetime, until: datetime
    ) -> list[Deed]:
        """她 ``[since, until)`` 之间做过、说过的事，按提交序。"""
        ...

    def own_line(self, deed: Deed) -> str:
        """她自己那条经历摆到她眼前的样子（她的一轮和日记材料里是同一个样子）。"""
        ...

    async def marks(self, *, lane: str, persona_id: str) -> tuple[int, int]:
        """她此刻最新的经历号和位置号（``(happening_seq, whereabouts_seq)``，没有是 0）。"""
        ...

    async def after(
        self, *, lane: str, persona_id: str, marks: tuple[int, int]
    ) -> SinceMarks:
        """``marks`` 之后她的经历和位置。"""
        ...

    async def last_spoke_in(
        self, *, lane: str, persona_id: str, channel_id: str
    ) -> datetime | None:
        """她上次在这条会话上开口的时刻；从没说过返回 ``None``。"""
        ...


# ---------------------------------------------------------------------------
# pictures：她自己做过的图（pictures 插件提供）
# ---------------------------------------------------------------------------


class HerPicture(Protocol):
    """她手上的一张图，交给别的插件时用得到的那一点：它在对象存储里的位置。"""

    file_name: str


class Pictures(Protocol):
    """她自己做过的那些图。手机和嘴经它认她的图：认法、印法跟她那几只看图的手是同一份。"""

    async def hers_among(
        self, *, lane: str, persona_id: str, file_names: Collection[str]
    ) -> set[str]:
        """``file_names``（对象存储里的位置）里哪几个是她在这条泳道上手上真有的图。"""
        ...

    async def her_picture(
        self, *, lane: str, persona_id: str, picture_id: str
    ) -> HerPicture | None:
        """她在这条泳道上手上的这一张；不是她的、或者不在这条泳道上，返回 ``None``。"""
        ...

    def printed_handle(self, file_name: str) -> str:
        """这张图印在她眼前的句柄（``pic=<id>``），她照抄回来的就是这一串。"""
        ...

    def picture_id_in(self, what_she_copied: str) -> str:
        """她照抄回来的那串认回图的 id（带不带 ``pic=`` 前缀都认）。"""
        ...


# ---------------------------------------------------------------------------
# phone：她的手机（phone 插件提供）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Reachable:
    """她手机上的一条会话：地址、是私聊还是群、叫什么、用哪个 bot 身份说话。

    ``bot_name`` 在这里就解析出来，不留到发消息的时候再查：出站的身份必须是确定的
    （主动发不写 ``common_agent_response``，worker 没有别处可推断用哪个 bot），
    而"她能不能听见"和"她用什么身份说话"本来就是同一件事的两面。
    """

    channel_id: str
    scope: str        # 'direct'（私聊） | 'group'（群）
    title: str
    channel: str      # 渠道（lark / qq / ...），出站段原样带走
    bot_name: str


@dataclass(frozen=True)
class Sender:
    """信封上点到的一个人：她见到的那个名字，和"这是不是主人"这个事实。

    **两件事必须分开带。** 名字是 ``sender_display_name``，改得动；``is_owner`` 是
    ``common_user`` 上那一列，改不动。把它们合成一个字符串（"bezhai（主人）"）就等于
    把身份交还给名字 —— 冒充者把昵称改成同样的字，输出一模一样。
    """

    name: str
    is_owner: bool
    is_bot: bool = False


@dataclass(frozen=True)
class Summons:
    """一条**在叫她**的消息：私聊来的，或者群里点了她的名。

    "强"不是发送方标的：私聊之所以强，是因为私聊本身意味着有人在等她回；群里点名之所以
    强，是因为那是直接叫她。除此之外没有分级、没有权重表——那是替她做决定。
    """

    message_id: str
    channel_id: str
    at: datetime


class Phone(Protocol):
    """她的手机：哪些会话在她手机上、谁在叫她、她在一条会话上说的话算哪一档、一个人摆在她眼前的样子。

    **哪些会话在她手机上只有一道闸**：她自己的 bot 还在，而且这条会话在她视野里。闸就在
    :meth:`reachable_conversations` 上，发消息、读文件问的都是这一份，别处不许自己拼一份；视野在她的
    一轮之内只定一次（一轮里多处各算一次的话，她看到的会话会在这一轮中途变化）。唯一不过视野那一半
    的是撤回（:meth:`conversation_her_bot_is_in`）。
    """

    async def reachable_conversations(
        self, *, persona_id: str, now: datetime
    ) -> list[Reachable]:
        """她此刻手机上的会话。``now`` 是调用方的时间锚：这一轮的 ``now``，或者那一拍的时刻。"""
        ...

    async def reachable_conversation(
        self, *, persona_id: str, channel_id: str, now: datetime
    ) -> Reachable | None:
        """她此刻手机上的某一条会话；不在她手机上返回 ``None``。"""
        ...

    async def conversation_her_bot_is_in(
        self, *, persona_id: str, channel_id: str
    ) -> Reachable | None:
        """她的 bot 还在的某一条会话，不管它此刻在不在她视野里；bot 已经不在了返回 ``None``。只给撤回用：
        她撤的是自己已经发出去的话，跟她现在还看不看得见那条会话是两件事。"""
        ...

    async def newest_unread_summons(
        self, *, lane: str, persona_id: str, now: datetime
    ) -> Summons | None:
        """她手机上还没看过、而且是在叫她的那条消息里最新的一条；没有返回 ``None``。"""
        ...

    def medium_for(self, scope: str) -> str:
        """这条会话（:attr:`Reachable.scope`）上说的话落成哪一档 medium。"""
        ...

    def who_tag(self, sender: Sender) -> str:
        """一个人摆在她眼前的样子（``<who from=".." rel="owner"/>``），跟她手机上的署名是同一套。"""
        ...


# ---------------------------------------------------------------------------
# received：传到她这里的消息（household 插件提供）
# ---------------------------------------------------------------------------


class Letter(Protocol):
    """传到她这里的一条消息（world 告诉她察觉到的、姐妹直接对她说的），交给别的插件时读得到的样子。
    ``message_time`` 是消息自带的时间：它说的事发生在什么时候，不是到达的时刻。"""

    message_id: str
    sender: str
    body: str
    message_time: datetime


class HerDue(Protocol):
    """她一轮的 due（life 判出它）里别的插件读得到的那一样：收件箱里叫醒她这一轮的那条消息的 id，
    没有是 ``None``。它还没看过就一定摆进这一轮（household 插件），这一轮落地它就算看过。"""

    must_show: str | None


class Received(Protocol):
    """传到她这里的消息：收件只存储，她醒来时读；摆进哪一轮、记没记成看过由 household 插件自己管。"""

    async def earliest_waking_unread(self, *, lane: str, persona_id: str) -> str | None:
        """她还没看过、发件方要叫醒她的消息里最早那条的 id；没有返回 ``None``。"""
        ...

    async def between(
        self, *, lane: str, persona_id: str, since: datetime, until: datetime
    ) -> list[Letter]:
        """她收到的、说的是 ``[since, until)`` 之间的事的那些，按消息自带的时间排，看没看过都算。"""
        ...

    def line(self, letter: Letter) -> str:
        """收到的一条在她眼里的样子（她每一轮读到的、日记材料里摆的是同一个样子）。"""
        ...


# ---------------------------------------------------------------------------
# day_pages：她给每个生活日写下的那一页（day_page 插件提供）
# ---------------------------------------------------------------------------


class DayPage(Protocol):
    """她给一个生活日写下的那一页，交给别的插件时读得到的样子。"""

    day: date
    text: str


class DayPages(Protocol):
    """她的日记：每个生活日她自己写下的一页。"""

    async def between(
        self, *, lane: str, persona_id: str, since: date, until: date
    ) -> list[DayPage]:
        """``[since, until)`` 这段日子里她写下的每一页，按日子升序；一页都没有返回空列表。"""
        ...


# ---------------------------------------------------------------------------
# loose_ends：她心里挂着没了结的事（loose_ends 插件提供）
# ---------------------------------------------------------------------------


class LooseEnds(Protocol):
    """她心里挂着、还没了结的那份清单。"""

    async def open_count(self, *, lane: str, persona_id: str) -> int:
        """她此刻还挂着几件。"""
        ...
