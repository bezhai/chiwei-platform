"""living 的两类持久数据：她做过的事、她在哪。两样都只属于她自己，也只给她自己读。

:class:`Happening` 和 :class:`Whereabouts` 是纯 append + 自然键幂等
（``insert_idempotent``），不声明 Version：它们记的是"已经发生过的事"和"某一轮她在
哪"，没有"改一条旧记录"的语义，重放（工具重试 / durable 重投）用同一个自然键再写
一次就该是无害的 no-op。

**字段一次想清楚**：migrator 是 additive-only，加列随时可以（``ALTER TABLE ADD
COLUMN``），删列 / 改类型会 ``MigrationError`` 崩启动。所以这里宁可少写一个字段
（以后加），也不写"可能有用"的字段（以后删不掉）；时刻一律用真正的时间类型，不用
文本——文本能顺利存进一条"下午三点"，然后让整批读取的 cast 一起失败。
``tests/living/test_registered.py`` 把列的形状钉住，改了会红。

lane 进 Key 是硬约束：runtime 持久化不给任何 Data 自动加 lane，不显式带上就会
和 prod 的行混在一张表里。

别的插件写进来、读出去的那几个取值（``kind``、``medium``、她发出去那条消息的 id 前缀）是插件之间的
约定，定义在 :mod:`app.living.services`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import field_validator

from app.data.schema import CommitOrder, Data, Key
from app.living.common import require_aware
from app.living.services import (
    KIND_ACT,
    KIND_SPEECH,
    MEDIUM_GROUP_CHAT,
    MEDIUM_IN_PERSON,
    MEDIUM_PHONE,
)

_KINDS = frozenset({KIND_SPEECH, KIND_ACT})
_MEDIA = frozenset({MEDIUM_IN_PERSON, MEDIUM_PHONE, MEDIUM_GROUP_CHAT})


class Happening(Data):
    """她做过的一件事：对谁、通过什么渠道、说了什么或做了什么。

    ``actor`` 就是她：每一行都是某一个人自己的经历，也只给那个人自己读（读的一侧每一条查询
    都按 ``actor`` 筛，``tests/living/test_only_her_own_records.py`` 守着）。别人能不能察觉到，
    不由这张表决定（见 :mod:`app.living.her_records.deeds`）。

    自然键 ``(lane, happening_id)``——重放同一个 ``happening_id`` 只落一行。

    ``seq`` 是**本 lane 内的提交序**，三个人共用一条轴，由
    :func:`app.data.ordered.insert_in_commit_order` 分配：号的先后 == 提交的先后，哪个进程
    写的都一样，可见的 seq 集合永远是一段连续前缀。读侧的游标因此可以放心推到"本次读到的
    最大 seq"，不会把一条还在飞的记录永久越过去（:class:`app.living.household.outgoing.OutgoingUpTo`）。
    读的一侧只读她自己的那几行，按这条轴排先后。
    **不要用 ``occurred_at`` 当游标**——同一轮里的几件钟点都一样，而常规那一轮还记着格子的
    旧记录里，提前来的那一轮和钟点上该来的那一轮落地的先后可以跟钟点反过来。

    ``audience`` 是"说给谁"，**可以是好几个人**：她自己在 ``say`` 的 ``to`` 里写下的名字。
    空 = 没有特定对象。做成列表而不是一个名字，是因为"同时对两个姐妹说一句话"是一件事：
    姐妹各收到一条直达的，这句话本身只记一次（:mod:`app.living.household.outgoing`）。

    ``channel_id`` 是**哪一条会话**（``common_conversation.common_conversation_id``），
    只有 ``phone`` / ``group_chat`` 这两个 medium 有；当面说的话是 ``None``。形状定成 common 口径的会话 id 而不是渠道裸 id（飞书 ``oc_*``），
    理由跟出站契约同一条：出站段的 ``chat_id`` 就是这个 id，接 QQ 时不用换形状。
    不带它的话，她下一轮只知道"我说过这句话"，不知道说给哪条会话——于是"你上次在
    这个群开口是什么时候"这条事实根本算不出来。
    """

    lane: Annotated[str, Key]
    happening_id: Annotated[str, Key]
    seq: Annotated[int, CommitOrder(within=("lane",))] = 0
    actor: str           # 谁的经历（persona_id）
    kind: str            # KIND_SPEECH | KIND_ACT
    medium: str          # MEDIUM_IN_PERSON | MEDIUM_PHONE | MEDIUM_GROUP_CHAT
    content: str         # 原话 / 做了什么，自然语言
    occurred_at: datetime  # 发生时刻，展示用，**不当游标**
    audience: list[str]  # 说给谁（可以多个）；空 = 没有特定对象
    # 哪条会话上说的（common_conversation_id）；None = 不在任何会话上（当面）。
    # 可空而不是空串：这是后加的列，``ALTER TABLE ADD COLUMN`` 给已有行留的是 NULL，
    # 声明成 ``str`` 会让那些行一读出来就 ValidationError。
    channel_id: str | None = None

    class Meta:
        # 两种读侧形状：
        #   * (lane, seq)          取下一个号的 MAX(seq)，和按提交序往后讲的那一段
        #   * (lane, occurred_at)  某一整个生活日（日记材料，一天三次）
        # 第二条按**发生时刻**开窗，跟提交序那条不是同一个问题：一天的边界是钟点，
        # 而 seq 是提交序。没有索引的话它会随着这张表一起变慢，而症状只是"日记这一轮
        # 有点久"。
        indexes = (("lane", "seq"), ("lane", "occurred_at"))

    # ``kind`` / ``medium`` 上面写着"机制层硬定的枚举"，这里让它真的是。
    # 不用 ``Literal`` 是因为 migrator 会把它映成 JSONB 列（``pg_type_for_annotation``
    # 对未知泛型 origin 的兜底），列类型一旦落地就改不回来了。
    #
    # 值得单独挡一下，是因为写错的表现完全是静默的：``medium="in-person"``（连字符）
    # 会走进"隔着设备"那一支，她当面做的事从此一件都不进 world 的汇总，日志里什么都没有。
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
        return require_aware("occurred_at", v)


class Whereabouts(Data):
    """她此刻在哪、在做什么。

    自然键 ``(lane, persona_id, moment_id)``：``moment_id`` 是写这条的那一轮的
    标识，让同一轮重放只落一行。纯 append——上一轮的位置留在表里，不是被覆盖。

    **只给她自己读**：她每一轮眼前那句"手上"、环顾四周时她告诉 world 自己在哪
    （:func:`app.living.moment.look_around`）、汇总末尾那句她在哪在做什么
    （:mod:`app.living.household.outgoing`）。别人在哪，world 按各人自己报的位置判断，life 不读。
    ``seq`` 同 :class:`Happening`（这里是 per-(lane, persona) 的轴），作用是让
    "最新一条"有唯一确定的答案，不靠 ``created_at`` 的同刻并列去猜。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    moment_id: Annotated[str, Key]
    seq: Annotated[int, CommitOrder(within=("lane", "persona_id"))] = 0
    place: str
    doing: str
    noted_at: datetime   # 记下这条的时刻

    class Meta:
        indexes = (("lane", "persona_id", "seq"),)

    @field_validator("noted_at")
    @classmethod
    def _aware_noted_at(cls, v: datetime) -> datetime:
        return require_aware("noted_at", v)
