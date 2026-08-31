"""living 的三类持久数据。

:class:`Happening` 和 :class:`Whereabouts` 是纯 append + 自然键幂等
（``insert_idempotent``），不声明 Version：它们记的是"已经发生过的事"和"某一缝她在
哪"，没有"改一条旧记录"的语义，重放（工具重试 / durable 重投）用同一个自然键再写
一次就该是无害的 no-op。

:class:`Upcoming` **有版本链**，因为它有一个真实的状态变化：写下 → 被拿走。理由写在
它自己的 docstring 里。用的是 framework 的 ``Version`` + ``insert_append`` CAS，不是
另起一张影子表——"这条拿走过没有"是这条日历项的状态，不是另一件事。

**字段一次想清楚**：migrator 是 additive-only，加列随时可以（``ALTER TABLE ADD
COLUMN``），删列 / 改类型会 ``MigrationError`` 崩启动。所以这里宁可少写一个字段
（以后加），也不写"可能有用"的字段（以后删不掉）；时刻一律用真正的时间类型，不用
文本——文本能顺利存进一条"下午三点"，然后让整批读取的 cast 一起失败。
``tests/living/test_registered.py`` 把列的形状钉住，改了会红。

lane 进 Key 是硬约束：runtime 持久化不给任何 Data 自动加 lane，不显式带上就会
和 prod 的行混在一张表里。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import field_validator

from app.runtime.data import Data, Key, Version

# happening 的形态。机制层硬定的两类，不是让模型自由发挥的字符串：
#   * ``speech``  说出口的话。``content`` 是原话。
#   * ``act``     做的事。``content`` 是一句自然语言描述（"我去厨房煮抹茶"）。
# 两者共用一张表，是因为「谁在哪对谁做了什么说了什么」在读取侧是同一件事——
# 旧引擎把说话和做事拆成两条通道，结果 world 看不见姐妹之间发生了什么。
KIND_SPEECH = "speech"
KIND_ACT = "act"
_KINDS = frozenset({KIND_SPEECH, KIND_ACT})

# 通过什么渠道。这是**客观事实**——她是当面说的，还是拿手机发的，还是发在群里的。
# 不是给她的行为分优先级，也不是强度分级：三个值之间没有高低，只有"声音能不能传到
# 旁边的人耳朵里"这一条物理差别。
#   * ``in_person``   当面说 / 当场做。同一地点的人听得见，同一栋别处知道有动静。
#   * ``phone``       私聊消息。隔着设备，旁边的人看不见，只有收件人收得到。
#   * ``group_chat``  群里说话。同上，只有群里的人（audience）收得到。
MEDIUM_IN_PERSON = "in_person"
MEDIUM_PHONE = "phone"
MEDIUM_GROUP_CHAT = "group_chat"
_MEDIA = frozenset({MEDIUM_IN_PERSON, MEDIUM_PHONE, MEDIUM_GROUP_CHAT})


def legacy_null_is(default: object):
    """给**后加的非 Optional 列**用的 before-validator：NULL 当成 ``default``。

    这是一个通用的部署顺序陷阱，不是某一列的特例。migrator 加列生成的是
    ``ALTER TABLE ... ADD COLUMN <type>``——**可空、不带 DB 默认值**；pydantic 那个
    ``= False`` 只是构造模型时的默认，跟列默认值没有半点关系。所以任何已经有数据的
    泳道，加完列之后旧行的新列全是 ``NULL``，读出来构造模型就 ``ValidationError``，
    整条链路推不动，而且报错发生在读取侧、离"我加了一列"很远。

    两条防线缺一不可：这个 validator 管**读出来构造得起来**，SQL 侧还要
    ``COALESCE(col, <default>)`` 管**按它过滤时旧行不被当成第三种值**（NULL 既不等于
    true 也不等于 false）。

    另一条路是把新列声明成 ``X | None``（``Happening.channel_id`` 走的就是这条），
    代价是每个读取方都要处理 ``None``。语义上真的可空就用那条；语义上"旧行等于某个
    默认值"就用这条。
    """

    def _coerce(cls: type, v: object) -> object:  # noqa: N805 — classmethod 形参
        return default if v is None else v

    return _coerce


def _require_aware(name: str, v: datetime | None) -> datetime | None:
    """时刻必须带时区；不带就当场炸。

    落进 TIMESTAMPTZ 的 naive datetime 会被按服务器时区解释，静默偏 8 小时——
    ``due_at`` 是整个日历的基准，偏了就是日历全错、而且一句报错都没有。跟
    ``medium`` 写成 ``"in-person"`` 是同一类静默毒化，所以挡在同一个位置。
    """
    if v is not None and v.tzinfo is None:
        raise ValueError(
            f"{name} 必须带时区：不带 tzinfo 的时刻落进 TIMESTAMPTZ 会被按服务器"
            f"时区解释，静默偏几个小时且不报错。收到 {v!r}"
        )
    return v


class Happening(Data):
    """一件已经发生的事：谁、在哪、对谁、通过什么渠道、说了什么或做了什么。

    自然键 ``(lane, happening_id)``——重放同一个 ``happening_id`` 只落一行。

    ``seq`` 是**本 lane 内的提交序**，由 :func:`app.living.serial.append_in_commit_order`
    在排他占用下分配：拿号和落库之间占用不放开，所以 seq 的先后 == 提交的先后，
    可见的 seq 集合永远是一段连续前缀。读侧的游标因此可以放心推到"本次读到的最大
    seq"，不会把一条还在飞的记录永久越过去。**不要用 ``occurred_at`` 当游标**——
    它是行为发生的时刻，跨 persona 并发时跟落库顺序无关，按它开窗必漏。

    ``audience`` 是"说给谁"，**可以是好几个人**：里面的人一定读到原话，跟位置无关
    （位置数据算错了也不许丢）。空 = 没有特定对象。做成列表而不是单个 persona，是
    因为"同时对两个姐妹说一句话"是一件事，复制成两条事件会让 seq、回声抑制、旁听
    裁剪各错一遍。

    ``who_was_where`` 是**事情发生那一刻**各人分别在哪（persona_id → 位置路径）的
    快照。旁听判档读的是它，不是读取时的最新位置：事件可能在她整轮模型调用期间提交，
    而她在缝末换了房间——用新位置去裁旧事件，在场的人会漏听、不在场的人反而听见。
    存"当时谁在哪"这个事实而不是存裁好的结果，是因为事实不会变、而三档规则可能改。

    ``channel_id`` 是**哪一条会话**（``common_conversation.common_conversation_id``），
    只有 ``phone`` / ``group_chat`` 这两个 medium 有；当面说的话和世界自己发生的事
    是 ``None``。形状定成 common 口径的会话 id 而不是渠道裸 id（飞书 ``oc_*``），
    理由跟出站契约同一条：出站段的 ``chat_id`` 就是这个 id，接 QQ 时不用换形状。
    不带它的话，她下一缝只知道"我说过这句话"，不知道说给哪条会话——于是"你上次在
    这个群开口是什么时候"这条事实根本算不出来。
    """

    lane: Annotated[str, Key]
    happening_id: Annotated[str, Key]
    seq: int
    actor: str           # 谁做的 / 说的（persona_id）
    place: str           # 发生在哪（层级路径，见 app.living.place）
    kind: str            # KIND_SPEECH | KIND_ACT
    medium: str          # MEDIUM_IN_PERSON | MEDIUM_PHONE | MEDIUM_GROUP_CHAT
    content: str         # 原话 / 做了什么，自然语言
    occurred_at: datetime  # 发生时刻，展示用，**不当游标**
    audience: list[str]  # 说给谁（可以多个）；空 = 没有特定对象
    who_was_where: dict[str, str]  # 发生时各人在哪的快照
    # 哪条会话上说的（common_conversation_id）；None = 不在任何会话上（当面 / 世界）。
    # 可空而不是空串：这是后加的列，``ALTER TABLE ADD COLUMN`` 给已有行留的是 NULL，
    # 声明成 ``str`` 会让那些行一读出来就 ValidationError。
    channel_id: str | None = None

    class Meta:
        # 读侧唯一形状：某 lane 下 seq 之后的一段。
        indexes = (("lane", "seq"),)

    # ``kind`` / ``medium`` 上面写着"机制层硬定的枚举"，这里让它真的是。
    # 不用 ``Literal`` 是因为 migrator 会把它映成 JSONB 列（``pg_type_for_annotation``
    # 对未知泛型 origin 的兜底），列类型一旦落地就改不回来了。
    #
    # 值得单独挡一下，是因为写错的表现完全是静默的：``medium="in-person"``（连字符）
    # 会走进"隔着设备"那一支，同屋的人从此一句都听不见，日志里什么都没有。
    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in _KINDS:
            raise ValueError(f"kind 只能是 {sorted(_KINDS)} 之一，收到 {v!r}")
        return v

    @field_validator("medium")
    @classmethod
    def _known_medium(cls, v: str) -> str:
        if v not in _MEDIA:
            raise ValueError(f"medium 只能是 {sorted(_MEDIA)} 之一，收到 {v!r}")
        return v

    @field_validator("occurred_at")
    @classmethod
    def _aware_occurred_at(cls, v: datetime) -> datetime:
        return _require_aware("occurred_at", v)


class Whereabouts(Data):
    """她此刻在哪、在做什么。

    自然键 ``(lane, persona_id, moment_id)``：``moment_id`` 是写这条的那一缝的
    标识，让同一缝重放只落一行。纯 append——上一缝的位置留在表里，不是被覆盖。

    ``place`` 是**客观事实**。旁听判档不在读事件时回来查它——
    :func:`app.living.happening.record_happening` 在写入事件的那一刻把"此刻谁在哪"
    拍进 :attr:`Happening.who_was_where`，之后这条位置再怎么变都不影响已经发生过的事。
    ``seq`` 同 :class:`Happening`（这里是 per-(lane, persona) 的轴），作用是让
    "最新一条"有唯一确定的答案，不靠 ``created_at`` 的同刻并列去猜。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    moment_id: Annotated[str, Key]
    seq: int
    place: str
    doing: str
    noted_at: datetime   # 记下这条的时刻

    class Meta:
        indexes = (("lane", "persona_id", "seq"),)

    @field_validator("noted_at")
    @classmethod
    def _aware_noted_at(cls, v: datetime) -> datetime:
        return _require_aware("noted_at", v)


class Upcoming(Data):
    """将要发生的一件客观事：什么时候、什么事、在哪、被谁消费掉了没有。

    自然键 ``(lane, item_id)``。日出日落、三餐、店关门这类客观时刻是数据、不是
    模型判断——写下来就行。

    ``due_at`` 是**真正的时间类型**，不是任意文本。文本的代价是双份的：一条
    "下午三点"能顺利落库，然后整个窗口的 cast 一起失败，她那一缝一条日历项都读
    不到；而且索引撑不起范围查询。类型 additive-only，改不回来，所以只能一开始就定对。

    这张表**没有 seq，有版本链**，跟另外两张不一样。它的消费不是"读到哪了"而是
    "这条拿走过没有"：``(after, until]`` 开窗只在"所有项必定提前写入"这个从没被
    编码过的假设下才对，重启补种 / 重试 / world 晚提交一条已经过了游标的 item 都
    会被永久越过。所以交付条件是 ``consumed_at IS NULL AND due_at <= now``，消费方
    拿走后 append 一版把 ``consumed_at`` 填上（``ver`` 由 framework 维护，CAS 保证
    并发下只有一个人标得掉）。到期之后要变成她能感知到的东西，是 T3 的事（形态上
    就是往 :class:`Happening` 里 append 一行）。
    """

    lane: Annotated[str, Key]
    item_id: Annotated[str, Key]
    ver: Annotated[int, Version]  # framework 维护的版本号，v1 = 写下，之后 = 消费掉
    what: str
    due_at: datetime     # 到期时刻
    place: str | None = None       # 在哪发生；None = 不绑定地点（天黑这种）
    consumed_at: datetime | None = None  # 被拿走的时刻；None = 还没被拿走

    # 不声明 Meta.indexes：读取形状是"每个 item 的最新一版"，先 DISTINCT ON
    # (lane, item_id) ORDER BY ver DESC 再筛 due_at —— 走的是 migrator 给 Version
    # 类自动建的 ix_key_ver。一条 (lane, due_at) 索引落在子查询外面，谁也用不上。

    @field_validator("due_at", "consumed_at")
    @classmethod
    def _aware_instant(cls, v: datetime | None) -> datetime | None:
        return _require_aware("due_at / consumed_at", v)
