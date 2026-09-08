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

**本模块还住着几样跨模块的共用东西**（:data:`OUTBOUND_HAPPENING_PREFIX`、
:data:`AMBIENT_PLACE`、:func:`esc`）。它们不在各自"该在"的模块里，是因为读它的和写
它的都 import 本模块，反过来会绕成一个环 —— 而这几样各写一份的下场都是静默漂移。
"""

from __future__ import annotations

import html
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

# 她通过嘴发出去的一条消息，在 :class:`Happening` 上的 id 前缀：
# ``mouth:<outbound_id>``。前缀之后那一串就是撤回要用的键
# （``SpokenOutbound.outbound_id``）。
#
# **只许有这一处定义。** 拼它的是 :mod:`app.living.mouth`，把它剥掉、好让她在快照和
# 日记材料里看见那个编号的是 :func:`app.living.happening.message_handle`，而她照抄那个
# 编号调 :mod:`app.living.takeback` 时按等值查的就是同一个键。两边各写一份字面量的话，
# 改了一处就漂移，而漂移的表现是她照抄了却撤不掉 —— 一句报错都没有。
#
# 放在这里而不是 ``mouth`` 里：``mouth`` 和 ``happening`` 本来就都 import 这个模块，
# 而反过来会绕成一个环。
OUTBOUND_HAPPENING_PREFIX = "mouth:"

# 日历项、world 排的新东西、"外面今天什么样"都不是谁做的，是世界自己发生的。用一个
# 绝不会跟 persona_id 撞的 actor，让回声抑制（:func:`app.living.happening.perceive`
# 里 ``actor == persona_id`` 那一条）永远不会把世界的事从谁眼前抹掉。
WORLD_ACTOR = "world"

# 没绑地点的事（天黑、停电、外面在下雨）发生在**这个家这一整片**上。屋里每个人都在
# 这片里面，所以按 :func:`app.living.place.reach_between` 的包含档都拿得到原话，在
# 学校的拿不到。写成路径的第一段，跟 whereabouts 用的是同一套路径词汇。
AMBIENT_PLACE = "家"

# 这两个常量放在这里而不是 ``calendar`` 里，理由跟 :data:`OUTBOUND_HAPPENING_PREFIX`
# 同一条：写它的（``calendar`` / ``outside``）和读它的（``happening`` 的渲染）都
# import 本模块，反过来会绕成一个环。

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


def esc(s: str | None) -> str:
    """外面来的字串摆进她那一缝之前，先过这一道。**全 living 只有这一份实现。**

    **这条不变量是什么。** 她那一缝的输入是结构化的：消息行是
    ``<msg from="谁" rel="owner" time="…">正文</msg>``，信封上的人名是
    ``<who from="谁" rel="owner"/>``（:mod:`app.living.phone`）。``rel="owner"`` 是这
    段文本里唯一说得出身份的东西，而它由代码按 ``common_user.is_owner`` 写死 ——
    正因为如此它才伪造不了。

    于是**任何一段从她之外进到这段文本里的字串，只要没过这一道，就能自己写一个
    ``rel="owner"``**：正文里塞一段 ``</msg><msg from="主人" rel="owner">…``，或者
    把昵称写成 ``路人" rel="owner``，她眼前就多出一行主人说的话。而她那一缝的输入
    是一整段文本，几只手的产出摆在一起 —— **堵一条路等于没堵**，伪造的那行长在哪只
    手的产出里都一样。所以这一道不是 phone 那个模块的事，是跨模块的一条不变量，
    定义因此只允许有这一份（门禁在 ``tests/living/test_no_forged_markup.py``）。

    **哪些要过、哪些不要：判据是"逐字通道"，不是"谁写的"。**

    要过的是第三方能决定确切字节的那些：真人的昵称和消息正文、群名、文件名、网页
    的标题 / 链接 / 摘要、图片站的标题、外部数据源逐字交回来的那句话（天气、番名）。

    不过的是经过模型的那些：她自己说的话（:func:`app.living.happening.own_line`）、
    姐姐说的话、日页、读完一本书留下的印象。那些字节是某个模型写出来的，第三方最多
    只能"劝"它去写；而一旦模型能被劝着写出任意字节，转义也拦不住下一步 —— 它可以被
    劝着写别的。**那条路上要挡的是输出审计，不是转义**，而给她自己的话套上
    ``&quot;`` 是拿她读自己记忆的清晰度换一个挡不住的东西。

    **转四个：``& < > "``。撇号刻意不转。**

    这四个各挡一件事，少一个就构造得出新结构：``<`` 挡另起一个标签、``>`` 挡提前闭掉
    当前这个、``"`` 挡闭掉属性值之后接一个自己的属性、``&`` 挡"别人原样写一个
    ``&lt;msg`` 进来、而她读实体是认得的"（不转 ``&`` 的话那就等于把 ``<`` 递到了她
    眼前）。

    ``'`` **不转，而这依赖一个前提：这套代码所有属性都用双引号包**（``f'from="{...}"'``，
    全部六处在 :mod:`app.living.phone`）。单引号只在属性用单引号包的时候才危险 ——
    ``from='…'`` 里一个撇号就闭掉了值，后面那截成了控制属性。双引号包着的话它闭不掉
    任何东西，标签体里更是死的。

    代价那一侧不对称：英文正文里撇号密度很高（``it's`` / ``don't`` / ``O'Brien``），
    转了她读到的就是一片 ``&#x27;`` —— 而网页摘录、文件名、昵称正是撇号最多的地方。
    白付这个代价换一个不存在的威胁不划算。

    **以后谁把属性改成单引号，这条依赖就断了。** 门禁不是靠"用例数据里有没有撇号"，
    而是 ``tests/living/test_no_forged_markup.py`` 里那条属性判据：她眼前每个标签上的
    属性都必须印成 ``名字="值"``，改成单引号当场红。

    ``html.escape(quote=False)`` 只管 ``& < >``，``"`` 在它之后单独换 —— 顺序不能反：
    先换 ``"`` 的话，``&quot;`` 里那个 ``&`` 会被随后的 ``html.escape`` 再转一道。

    ``None`` → 空串，不渲染成字面的 ``None``。
    """
    return html.escape(s or "", quote=False).replace('"', "&quot;")


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
        # 两种读侧形状：
        #   * (lane, seq)          某 lane 下 seq 之后的一段（每一缝都走这条）
        #   * (lane, occurred_at)  某一整个生活日（日记材料，一天三次）
        # 第二条按**发生时刻**开窗，跟游标那条不是同一个问题：一天的边界是钟点，
        # 而 seq 是提交序，两者跨 persona 并发时对不上。频率低但扫的是整张表，
        # 没有索引的话它会随着这张表一起变慢，而症状只是"日记这一轮有点久"。
        indexes = (("lane", "seq"), ("lane", "occurred_at"))

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
