"""chat 是她的嘴 —— 只有出口，没有入口。

life 产出「我想跟谁说个什么意思」，这里把它渲染成人话，发出去。

**为什么嘴要单独一个模型。** 一缝用的 ``life-model`` 推理强、对话差；拿它写对外的话，
出去的是一段推理稿（旧引擎实测：堆黑话、叫「主人」）。所以嘴用 ``main-chat-model`` +
自己的一份 prompt，跟 life 那一缝彻底分开。

**没有入口。** 这个模块里没有任何 ``@node``、没有 ``Source``、没有队列消费者，一次都
不出现旧 chat 入站链的名字。「被 @ 不能触发 chat」不是靠哪个分支里的 if 拦住的——是
**这里根本没有接消息的地方**。``tests/living/test_no_inbound.py`` 把这条钉死。

**出站照主动发那条契约走**（:mod:`app.domain.chat_dataflow` 的注释是单一定义处）：
``is_proactive=True``、``message_id`` 是 ``proactive:`` 前缀的本地派生键、``root_id``
留空。worker 据 ``is_proactive`` 走**不反查来源消息**的分支，直接用 ``chat_id``
（= 真实 ``common_conversation_id``）+ ``bot_name`` 投递。

**去重必须挡在出站之前，不能指望下游。** 实测过 chat-response-worker
（``apps/channel-server/src/workers/chat-response-handler.ts``）：

  * ``:193-207`` 自述"MQ redeliver **不做发送级去重**"——同一个 ``message_id`` 投两次
    就是真人收到两条；
  * ``:332`` 整个 handler **无条件 ack**，连出站失败也 ack（``:299-329`` 的 catch 只
    记日志）——所以那一侧既不会重投、也不会补发。

而 ``ChatResponseSegment`` 在这边是 ``transient``，sink 只管 publish，本身也没有去重。
所以稳定的派生 id 只解决了"两条长得一样"，没解决"发了两次"：真正那道闸是下面这张
:class:`SpokenOutbound` 认领表——工具重试、整轮 ``@retry`` 重放、这一缝重跑，全撞同一
个派生键、只出站一次。

**「她说过」和「真交出去了」的先后语义（崩在中间时哪一边留下）。** 这一段跨了 broker
和数据库，做不到原子；做得到的是**可预期、可查**。顺序钉死为：

  1. 认领（``claimed``）→ 2. ``emit`` 交给投递 → 3. 落 ``Happening``（她的记忆）→
  4. 收口（``handed_off``）。

  * ``emit`` **返回**了 → 3、4 照走，收口成 ``handed_off``。
  * 其余全部落进同一格：**结果未知**，行停在 ``claimed``、不留记忆、不重发。
    :func:`unsettled_outbound` 一句查询就能捞出来给人看。

**``emit`` 抛错不是「确定没发出去」，是「不知道」。** 这一条曾经判反过：抛错记
``handoff_failed`` 然后允许重试。但 publisher confirm 超时、连接断在确认之前，
**broker 都可能已经收件了**；而下游 chat-response-worker 出站失败照样 ack、MQ 重投
也没有发送级去重（下面那两处行号）。所以"抛错就自动重试"= 可能重复发给真人。

**重复发给真人比丢一条更糟**——这是设计决定，不是疏忽。所以未知的时候选不发：崩在
2 和 4 之间是这样，``emit`` 抛错也是这样，同一格，同一个待遇。

**不重发不等于把这句话判死。** ``outbound_id`` 从 ``(lane, persona, 这一缝, 会话,
她那句意图)`` 派生 —— 下一缝是新的 ``moment_id``、就是新的 ``outbound_id``，认领表
拦不住它。所以"要不要再说一次"这个决定回到了**她**手里，而不是系统替她按重试按钮。
这也是为什么这里没有重试计数器、没有退避、没有超时阈值：那些东西是替她做决定。

**她说出去的话要落一条 Happening。** 不落的话她下一缝不知道自己说过什么，于是对同一
件事又说一遍——旧引擎在 prod 上实锤复现过：两条主动发相隔三分钟、对同一件事说法前后
矛盾。落的是**真的发出去那句**（渲染后的），不是她那句内部意图。

**渲染没出内容就不发。** 不回退发意图原文（那是她脑子里的措辞，不是人话），也不发空
消息——把"没发出去"作为工具结果喂回她，她自己决定重说还是算了。

**交出去之前先过一道检查，不合格就不发。** 真人问她、她答的那条链是"先发后撤"（流式
回复没有同步检查的窗口）；她主动开口没有这个压力，所以检查放得进发送之前 —— 拦下的话
真人一个字都没看见，不用撤。判据在 :mod:`app.capabilities.output_safety`，两条链共用
同一份。

  * 位置在**渲染之后、认领之前**：判的是真人会看见的那句人话；拦下时不占那个 id，
    否则同一缝里她再说同一件事会撞上"你已经说过了"——而那是假话。
  * **这一关自己坏了的时候照发**（超时、模型挂了、词表读不到）。挡下来挡的不是一条
    消息、是整条线：她和三个姐妹一起哑掉，挂多久哑多久，而这道检查的实测拦截率本来
    就很低。用一个大且显眼的故障换一个小且罕见的风险不划算。代价是它**静默**，所以
    每漏一条都打一行 ``living_mouth_unchecked`` —— 那是数"那段时间漏了多少"的唯一
    锚，改措辞前先想清楚谁在数它。

**图不走正文，走自己的字段，而且算进发送身份。** 渲染那一步是**自由生成**，没有任何
原样保留的通道（prompt 明写"把它说成你会说的那句话"），图片引用混在 ``what`` 里必然
被改写或丢掉 —— 两种下场都不报错。所以图是 :func:`send_message` 的结构化参数，在
:class:`app.domain.chat_dataflow.ChatResponseSegment` 上有自己的一列，传的是对象存储
的**永久句柄**（地址 1.5 小时就死，签名由投递侧在最靠近发送的那一刻现签）。同时图要
算进 ``outbound_id`` 的派生：不算的话，同一缝同一条会话说同样的话配另一张图会被认领
表判成重发挡掉 —— 她换了张图重发，真人什么都收不到。

**第一版是单次渲染。** 她一缝里可以调好几次说好几条，但不能自己接着聊下去（没有对话
窗口自主权）——那是下一版的事。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from pydantic import Field, field_validator
from sqlalchemy import text

from app.agent.context import AgentContext
from app.agent.core import AgentConfig
from app.agent.neutral import Message, Role
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.capabilities.agent import AgentRunner
from app.capabilities.output_safety import audit_output
from app.data.queries.persona import find_persona
from app.data.session import get_session
from app.domain.chat_dataflow import (
    PROACTIVE_MESSAGE_ID_PREFIX,
    ChatResponseSegment,
)
from app.living.happening import record_happening
from app.living.phone import (
    conversation_as_she_knows_it,
    medium_for,
    reachable_conversation,
)
from app.living.pictures import her_picture, picture_id_in
from app.living.records import (
    KIND_SPEECH,
    OUTBOUND_HAPPENING_PREFIX,
    _require_aware,
)
from app.living.scope import moment_scope, note_recorded
from app.living.whereabouts import current_whereabouts
from app.runtime.data import Data, Key, Version
from app.runtime.emit import emit
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_append, select_latest

logger = logging.getLogger(__name__)

# Langfuse prompt id（新 id，只发泳道 label，不碰 production）。
LIVING_CHAT_VOICE_PROMPT_ID = "living_chat_voice"

# main-chat-model：这一步要的**只有**对话能力——把一个意思说成她会说的那句话。
# recursion_limit 1：嘴没有工具，一次生成就该收口。
_VOICE_CFG = AgentConfig(
    LIVING_CHAT_VOICE_PROMPT_ID,
    "main-chat-model",
    "living-chat-voice",
    recursion_limit=1,
)

# 派生出站 id 的命名空间，随手换会让历史消息全部对不上。
_ID_NS = uuid.UUID("d4a91f62-7c05-4b3e-9a18-2f6e8c07b5d1")

# 交出去之前那一关最多占多久。判词是一次模型调用，挂住了就是把她整缝卡在网络上 ——
# 一缝里她还有别的事要做。20s 沿用真人说话那侧检查的期限，不另立一个数。
#
# **这是护栏，不是替她做决定**：它管的是"这一步最多占用多久"，跟上面那段"没有重试
# 计数器、没有退避、没有超时阈值"不冲突 —— 那句说的是不替她决定要不要再说一次。
_SEND_CHECK_TIMEOUT_S = 20.0

# 认领记录的两种状态。**没有"确定没发出去"这一档**——投递这一步只可能"确定发过"
# 或者"不知道"，没有第三种（见模块 docstring）。
#   claimed     交给投递了、没等到收口。**结果未知：可能已经送到真人手上。**
#   handed_off  交出去了、记忆也落了，这条收口了。
STATE_CLAIMED = "claimed"
STATE_HANDED_OFF = "handed_off"


class SpokenOutbound(Data):
    """她交给投递的一条消息：认领 → 交出去 → 收口。

    **存在的理由只有一个：下游不去重。** chat-response-worker 对 MQ redeliver 不做
    发送级去重（chat-response-handler.ts:193-207 的自述），而且无条件 ack
    （同文件 :332）——同一段投两次，真人就收到两条。所以那道闸只能在这边，而且必须
    挡在 ``emit`` **之前**。

    自然键 ``(lane, outbound_id)``；``outbound_id`` 从 ``(lane, persona, 这一缝,
    会话, 她那句意图)`` 派生，所以工具重试、整轮 ``@retry`` 重放、这一缝重跑全撞同
    一条记录。

    **有版本链**，因为它有真实的状态变化（认领 → 收口），跟
    :class:`app.living.records.Upcoming` 同一个理由。

    **认领过就不再交第二次，一种状态都不例外。** ``emit`` 抛错也算认领过 —— 抛错
    只说明我们没等到确认，不说明 broker 没收到（见模块 docstring）。这一缝里她那句
    意图从此只对应这一条记录；要再说一次，得是下一缝（新的 ``moment_id`` = 新的
    ``outbound_id``），而那是**她**的决定。

    **落地是独立的一根轴，不是 ``state`` 上的第三档。** 本地认领（``claimed`` /
    ``handed_off``）说的是"这个进程走到哪一步了"，落地说的是"渠道上真的写下了哪一
    行"。两件事都真、也都可能单独发生：交出去了、渠道写下了、而进程崩在记忆那一步
    之前——这一条的事实就是 ``claimed`` **且**已落地。把落地压进 ``state`` 就会用一
    个 ``delivered`` 盖掉 ``claimed``，:func:`unsettled_outbound` 从此捞不出它，"她
    可能不记得自己说过"这件事再没人看得见。所以是两个独立字段。

    **这条链上有两个写者。** 一个是 :func:`send_message`（认领 → 收口），另一个是
    :mod:`app.living.landing` 那条对账钟（补落地那两列）。两边各写自己那根轴，但
    追的是同一条版本链，所以谁都可能先到 —— 每一次 append 都必须看 CAS 的返回值，
    并且**输了的那一方要把自己那根轴合到最新一版上**，不是照着手里那份过期的重写
    （见 :func:`_settle`）。
    """

    lane: Annotated[str, Key]
    outbound_id: Annotated[str, Key]
    ver: Annotated[int, Version]
    persona_id: str
    channel_id: str
    moment_id: str
    said: str            # 真的交出去那句话（渲染后的），不是她那句内部意图
    state: str
    claimed_at: datetime
    settled_at: datetime | None = None
    # 这次开口落地成了公共层的哪一行（``common_message.common_message_id``），由
    # :mod:`app.living.landing` 事后按 ``outbound_id`` 对账补上。
    #
    # **必须可空、且不能有 DB 默认值**：migrator 加列生成的是可空无默认的
    # ``ADD COLUMN``，已有行读回来就是 NULL；声明成不可空的话，加列之后这张表上每
    # 一条旧记录一读就 ValidationError。NULL 的含义只有一个：还没对上账 —— 可能是
    # 渠道还没写、可能是那次 ``emit`` 的结果本来就未知。
    landed_common_message_id: str | None = None
    # 渠道那一行自己的 ``event_time``（即她那句话真正落地的时刻），不是对账这一拍
    # 跑的时刻——后者只是钟的节拍，不是关于她那句话的事实。
    landed_at: datetime | None = None
    # **撤回是第三根轴。** 她按下撤回（``took_back_at``）和渠道那边真的撤掉
    # （``recalled_at``）是两件事，中间隔着一趟队列和一次渠道接口调用，都可能失败。
    #
    # 只有 ``took_back_at`` 非空 = 她去撤了，还没确认撤掉。这**不是**"撤失败了"：
    # 撤回是异步的，没有一个确定的失败信号（投递侧退避重投三次才进死信）。她下一缝
    # 拿起手机，撤掉了的那条就不在了，没撤掉的还在 —— 她自己看得出来。
    #
    # 跟上面两根轴同一个理由：不能压进 ``state``，也不能省成一个布尔。
    took_back_at: datetime | None = None
    recalled_at: datetime | None = None

    @field_validator(
        "claimed_at", "settled_at", "landed_at", "took_back_at", "recalled_at"
    )
    @classmethod
    def _aware_instant(cls, v: datetime | None) -> datetime | None:
        return _require_aware(
            "claimed_at / settled_at / landed_at / took_back_at / recalled_at", v
        )


_OUTBOUND_TABLE = _table_name(SpokenOutbound)


async def latest_outbound(
    *, lane: str, moment_id: str
) -> SpokenOutbound | None:
    """这一缝里那条出站记录的最新一版（一缝一条时够用，测试和排查用）。"""
    sql = (
        f"SELECT * FROM ("
        f"  SELECT DISTINCT ON (lane, outbound_id) * FROM {_OUTBOUND_TABLE} "
        f"  WHERE lane = :lane AND moment_id = :moment_id "
        f"  ORDER BY lane, outbound_id, ver DESC"
        f") latest ORDER BY claimed_at DESC LIMIT 1"
    )
    async with get_session() as s:
        row = (
            await s.execute(text(sql), {"lane": lane, "moment_id": moment_id})
        ).mappings().first()
    if row is None:
        return None
    return SpokenOutbound(**{k: row[k] for k in SpokenOutbound.model_fields})


async def unsettled_outbound(
    *, lane: str, persona_id: str
) -> list[SpokenOutbound]:
    """交出去了但没收口的那些 —— **崩在中间时留下的就是它们**。

    语义是"可能已经送到真人手上，而她可能不记得自己说过"。这一段跨了 broker 和数据
    库、做不到原子；能做到的是让残留可查。日常盘点或者事后排查直接查这个函数。
    """
    sql = (
        f"SELECT * FROM ("
        f"  SELECT DISTINCT ON (lane, outbound_id) * FROM {_OUTBOUND_TABLE} "
        f"  WHERE lane = :lane AND persona_id = :persona_id "
        f"  ORDER BY lane, outbound_id, ver DESC"
        f") latest WHERE state = :state ORDER BY claimed_at ASC"
    )
    async with get_session() as s:
        rows = (
            await s.execute(
                text(sql),
                {"lane": lane, "persona_id": persona_id, "state": STATE_CLAIMED},
            )
        ).mappings().all()
    return [
        SpokenOutbound(**{k: r[k] for k in SpokenOutbound.model_fields})
        for r in rows
    ]


def _already_claimed(tried: SpokenOutbound) -> str:
    """这句话已经在认领表上了 —— 回给她的那句话。

    **哪种状态都不再交第二次**：要么确定发过了，要么不知道发没发。不知道的时候选
    不发 —— 重复打扰真人比少一条更糟。措辞照实分开，因为"说出去了"和"不知道到没
    到"是两件不同的事，把后者说成前者就是编。

    两个地方用同一份措辞：出站前的预检查，和认领 CAS 被人抢走那一格。两格的事实
    是同一件（这一句已经有人交出去了、这一缝不会再发），所以话也只写一遍。
    """
    if tried.state == STATE_HANDED_OFF:
        return (
            f"这句话你已经说出去了（「{tried.said}」），没有再发一遍 —— "
            f"想说别的就换一句。"
        )
    return (
        f"这句话你已经交出去了（「{tried.said}」），但没等到确认，"
        f"不知道对方收没收到。没有再发一遍。"
    )


async def _settle(claim: SpokenOutbound, *, at: datetime) -> None:
    """收口：把 ``state`` 这一轴合到版本链**最新那一版**上。

    :mod:`app.living.landing` 那条对账钟是这条链上的第二个写者，而且下面这个时序
    是可达的：认领写下 v1 → ``emit`` → 渠道落账 → **对账钟先跑**，基于 v1 追了 v2
    （补上落地那两列，``state`` 仍是 ``claimed``）→ 收口这才走到，手里那个版本号
    已经过期。

    收口写不进去的后果不是"少记一笔"，是这条记录**永久停在「已落地、未收口」**：
    对账下一拍看它已经有落地标识、不再碰它，而 :func:`unsettled_outbound` 一直把
    它当"她可能不记得自己说过"捞出来。全程零报错。

    所以撞上了就重读最新一版，把 ``state`` / ``settled_at`` 合上去再写 —— 落地那
    两列照最新一版原样带走，绝不用手里那份过期的把已知事实抹回 NULL。

    **这是 CAS 循环，不是重试计数。** 没有次数上限、没有退避、没有"失败 N 次就放
    弃"：另一个写者在这条链上只推得动一版（落地补上之后这条就不在它的待办里了），
    所以循环一定收敛，而"放弃"在这里根本没有对应的事实 —— 话已经交出去了，收口这
    一笔要么写下，要么这条记录就永远是错的。

    **一个字都不许往外抛。** 走到这里 ``emit`` 已经返回了，抛出去会被
    :func:`tool_error` 转成"这条消息没发出去"喂回给她 —— 那是假话，而且会让她重
    发。丢一个状态比重复打扰真人轻。
    """
    current = claim
    while True:
        # 走构造函数而不是 ``model_copy(update=...)``：后者在 pydantic v2 上完全
        # 跳过校验，naive 的 ``settled_at`` 会从这条缝里溜进 TIMESTAMPTZ 列。
        # ``ver`` 由 ``insert_append`` 按 ``expected_current_ver`` 赋值。
        written = await insert_append(
            SpokenOutbound(
                **{
                    **current.model_dump(),
                    "state": STATE_HANDED_OFF,
                    "settled_at": at,
                }
            ),
            expected_current_ver=current.ver,
        )
        if written == 1:
            return
        latest = await select_latest(
            SpokenOutbound,
            {"lane": current.lane, "outbound_id": current.outbound_id},
        )
        if latest is None:
            # CAS 说这个键上已经有别的版本了，所以这里一定读得回来；真读不回来只
            # 能是有人把整条链删了。没有可以合并的对象，说出来就停 —— 不抛。
            logger.error(
                "living mouth lane=%s outbound=%s 收口时整条认领链读不回来了，"
                "这次开口的状态就此丢失",
                current.lane,
                current.outbound_id,
            )
            return
        assert isinstance(latest, SpokenOutbound)
        logger.info(
            "living mouth lane=%s outbound=%s 收口时版本被对账抢先了（ver %d → %d），"
            "合到最新一版上重写",
            current.lane,
            current.outbound_id,
            current.ver,
            latest.ver,
        )
        current = latest


def build_voice_runner() -> AgentRunner:
    """嘴的 agent。模块级函数，测试替身从这里换掉，不碰真模型。"""
    return AgentRunner(_VOICE_CFG, tools=None)


def _scene(scope: str, title: str) -> str:
    return f"你在群「{title}」里说话。" if scope != "direct" else f"你在跟「{title}」私聊。"


@tool
@tool_error("这条消息没发出去")
async def send_message(
    what: Annotated[
        str,
        Field(description="你想说的意思，一句话就行——措辞不用你操心"),
    ],
    channel_id: Annotated[
        str, Field(description="发到哪条会话，用信封 / 看手机时那串 channel_id")
    ],
    pictures: Annotated[
        list[str] | None,
        Field(
            description="要跟这条一起发出去的图，填它们的句柄（看图那边给你的那串），"
            "照抄；只说话就别填"
        ),
    ] = None,
) -> str:
    """给手机上的某条会话发一条消息。

    你给的是**意思**，不是原话：怎么说出口这一步不用你操心。

    只能发你手机上有的会话（信封上列着的那些），channel_id 照抄，别自己编。

    要带图就填 pictures，**别把图写进话里**——你写在话里的图片引用到不了对方那儿。
    只能带你自己做过的图。

    一缝里可以发好几条。但发完就是发完了——对方回了什么，要等你下一次拿起手机才知道。

    Args:
        what: 你想说的意思。
        channel_id: 发到哪条会话。
        pictures: 要一起发出去的图的句柄。

    Returns:
        这条的下场，附上真的说出口那句话。有时候只能告诉你"交出去了但不知道到没到"
        —— 那就是真的不知道，别当成发出去了，也别当成没发。也可能这句话过不了、
        压根没发出去，那就换个说法或者换件事说。
    """
    lane, now, persona_id, moment_id = moment_scope()
    intent = what.strip()
    if not intent:
        raise ValueError("说点什么：意思不能是空的。")

    # 这一道是**这一缝定下的那份名单 ∩ 实时 presence**，两半的时效性刻意不同：
    #
    #   * 名单读的是这一缝的锚（:mod:`app.living.whitelist`）。她要回的就是这一缝开
    #     头摆在她眼前的那些会话；中途因为时间窗滑过去而掉出名单，不该让她张嘴到一
    #     半发现刚看的那条会话没了。
    #   * presence 每次重新查（:func:`reachable_conversation` 里那次
    #     ``find_conversations_with_persona_bot``）。"bot 还在不在那个会话里"是这句
    #     话**能不能真的送到**的物理前提，读一份缝开头的快照等于这道保护不存在 ——
    #     bot 半路被移出会话，她还会照着过期的判断把话交出去。
    conv = await reachable_conversation(
        persona_id=persona_id, channel_id=channel_id.strip(), now=now
    )
    if conv is None:
        # fail-loud，绝不伪造一个地址：伪地址的表现是"发出去了"然后石沉大海。
        raise ValueError(
            f"{channel_id!r} 不是你手机上的会话，发不了 —— 用信封上那串 "
            f"channel_id，照抄。你能发的严格等于你看得见的那些会话。"
        )

    # 句柄换成永久句柄，就在这里。``lane`` + ``persona_id`` 是**硬条件**：句柄是从
    # ``file_name`` 派生的，别的泳道上那张同名的图算出来的是同一串，只按句柄取的话
    # 一个从别处拿到的串就能把姐姐画的、或者 prod 上那张取出来当成自己的发出去。
    #
    # 取不到就整条不发，**绝不跳过那一张把剩下的发出去**：她说的是"把这几张给他看"，
    # 少一张的那条消息不是她要发的那条。这一步在派生 id 之前，所以被挡下的那次不占
    # 认领 —— 她同一缝里换成自己那张图还发得成。
    file_names: list[str] = []
    for handle in pictures or []:
        # 认回来那一步走 :func:`picture_id_in`，**不在这里自己解析**：印给她的是
        # ``pic=<id>``，每只手都在叫她原样抄回来，这里认裸 id 的话她照做就撞死路。
        picture = await her_picture(
            lane=lane, persona_id=persona_id, picture_id=picture_id_in(handle)
        )
        if picture is None:
            raise ValueError(
                f"{handle!r} 不是你做过的图，发不了 —— 用看图那边给你的那串句柄，"
                f"照抄。你能发的严格等于你自己做出来的那些。"
            )
        file_names.append(picture.file_name)

    # **图算进发送身份。** 不算的话，同一缝、同一条会话、同样的话配另一张图会撞上
    # 同一个 ``outbound_id``，被下面那道认领闸判成重发直接挡掉 —— 她换了张图重发，
    # 真人什么都收不到，而她以为发了。选把图算进 seed，**不是**绕开去重：那道闸漏
    # 一次就是真人收到两条。
    #
    # 图那一段是**追加**上去的，所以不带图时拼出来的仍是从前那个字符串，历史上派生
    # 过的每一个 id 都还对得上（``tests/living/test_mouth.py`` 钉了一个写死的快照 ——
    # 改坏了的症状是认领表在历史记录上全部失效，而不是任何一条报错）。顺序也算进去：
    # 换个顺序发出去的就是另一条消息。
    seed = (
        f"{lane}\x1f{persona_id}\x1f{moment_id}\x1f{conv.channel_id}\x1f{intent}"
        + "".join(f"\x1f{name}" for name in file_names)
    )
    # 派生自**意图**而不是渲染结果：模型每次措辞可能不同，而重放同一次发送必须落回
    # 同一个 id，否则整轮重试会真的发出两条。
    derived = uuid.uuid5(_ID_NS, seed)
    outbound_id = derived.hex
    message_id = f"{PROACTIVE_MESSAGE_ID_PREFIX}{derived}"

    # **去重挡在渲染和出站之前。** 下游没有发送级去重（见模块 docstring 里 worker 那
    # 两处行号），所以这条闸漏一次就是真人收到两条。放在渲染之前还顺手省掉一次白花的
    # 模型调用。
    tried = await select_latest(
        SpokenOutbound, {"lane": lane, "outbound_id": outbound_id}
    )
    if tried is not None:
        assert isinstance(tried, SpokenOutbound)
        return _already_claimed(tried)

    persona = await find_persona(persona_id)
    known = await conversation_as_she_knows_it(
        lane=lane, persona_id=persona_id, channel_id=conv.channel_id, now=now
    )
    reply = await build_voice_runner().run(
        [
            Message(
                role=Role.USER,
                content=(
                    f"{_scene(conv.scope, conv.title)}\n\n"
                    f"这条会话上你已经知道的：\n{known}\n\n"
                    f"你现在想说的意思：{intent}\n\n"
                    f"把它说成你会说的那句话，直接给话，别解释。"
                ),
            )
        ],
        prompt_vars={
            "persona_name": getattr(persona, "display_name", "") or persona_id,
            "persona_core": (getattr(persona, "persona_core", "") or "").strip(),
        },
        context=AgentContext(
            persona_id=persona_id,
            session_id=f"living-mouth:{lane}:{persona_id}:{conv.channel_id}",
        ),
        max_retries=1,
    )
    said = reply.text().strip()
    if not said:
        # 只报事实。「没发出去」由 @tool_error 的前缀说完了，这里补的是为什么；
        # 接下来重试、换个说法、还是转头去干别的，是她的判断，工具不替她安排。
        raise RuntimeError("渲染没出内容")

    # 交出去之前先过这一关。位置钉死在**渲染之后、认领之前**：
    #
    #   * 渲染之后 —— 判的必须是真人会看见的那句人话，不是她脑子里那个意思。
    #   * 认领之前 —— 拦下时不占那个 id。占了的话，同一缝里她再说同一件事就会撞上
    #     "你已经说过了"，而那是假话：她一个字都没说出去。
    #
    # 而去重那道闸在这一关**更前面**（上面那段预检查），所以工具重试和整轮重放不会
    # 各花一次模型调用去判一句根本不会再发的话。
    verdict = await audit_output(said, timeout_s=_SEND_CHECK_TIMEOUT_S)
    if not verdict.ok:
        logger.warning(
            "living_mouth_blocked lane=%s persona=%s outbound=%s reason=%s"
            " 这句话没交出去",
            lane,
            persona_id,
            outbound_id,
            verdict.reason,
        )
        # **不抛。** 抛出去会被 :func:`tool_error` 转成一个错误喂回她，而错误她会重
        # 试 —— 重试会换一套措辞再判一次，那是一条绕过去的路。拦下是一个确定的结果，
        # 照实说就行。不给她具体是哪个词、哪一条判据：那等于把判据交出去让她绕。
        return (
            f"「{said}」—— 这句话没发出去，内容过不了这一关。"
            f"换个说法，或者换件事说。"
        )

    # 顺序钉死：认领 → 交出去 → 记忆 → 收口。每一步崩掉留下什么写在模块 docstring 里。
    # 认领永远是这条链的第一版：认领过就走不到这儿（上面直接返回），所以从零起。
    # 之后这条链上还会有对账钟写的版本，收口不能假定自己写的就是 v2（见 :func:`_settle`）。
    claim = SpokenOutbound(
        lane=lane,
        outbound_id=outbound_id,
        ver=1,
        persona_id=persona_id,
        channel_id=conv.channel_id,
        moment_id=moment_id,
        said=said,
        state=STATE_CLAIMED,
        claimed_at=now,
    )
    if await insert_append(claim, expected_current_ver=0) != 1:
        # **认领没抢到就绝对不能 ``emit``。** 有人在预检查之后、这一步之前认领了
        # 同一句话（同一缝的重入、同一泳道的两个进程），而他可能已经交出去了 ——
        # 下游没有发送级去重，这里照发就是真人收到两条。这道闸存在的全部意义就是
        # 挡住这一格，所以返回值必须看。
        taken = await select_latest(
            SpokenOutbound, {"lane": lane, "outbound_id": outbound_id}
        )
        if taken is None:
            # CAS 说这个键上已经有版本了，所以一定读得回来。真读不回来只能是有人
            # 把整条链删了 —— 那一格还没走到 ``emit``，说"没发出去"是真话，照抛。
            raise RuntimeError(
                f"认领 {outbound_id} 被别人抢走了，但抢走的那一版又读不回来"
            )
        assert isinstance(taken, SpokenOutbound)
        logger.info(
            "living mouth lane=%s persona=%s outbound=%s 认领被抢先了，不再交一次",
            lane,
            persona_id,
            outbound_id,
        )
        return _already_claimed(taken)

    if not verdict.checked:
        # fail-open：这一关自己坏了的时候照发。挡下来挡的不是一条消息、是整条线 ——
        # 她和三个姐妹一起哑掉，挂多久哑多久，而这道检查的实测拦截率本来就很低。
        # 代价是它**静默**，所以欠的这一笔必须留得下来：``living_mouth_unchecked``
        # 是数"那段时间漏了多少条"的唯一锚，改措辞前先想清楚谁在数它。
        #
        # **记在认领之后、``emit`` 之前**：认领抢输的那次一个字都没发出去，记进去
        # 这本账就虚了；而 ``emit`` 抛错的那次算数 —— 抛错只说明我们没等到确认，
        # broker 可能已经收件，那句没判过的话可能已经到了真人手上。
        logger.warning(
            "living_mouth_unchecked lane=%s persona=%s outbound=%s"
            " 这一关没判成，这句话按原样交出去了",
            lane,
            persona_id,
            outbound_id,
        )

    try:
        await emit(
            ChatResponseSegment(
                channel=conv.channel,
                message_id=message_id,
                persona_id=persona_id,
                part_index=0,
                session_id=None,          # 主动发没有 chat session
                chat_id=conv.channel_id,  # 真实会话地址
                is_p2p=conv.scope == "direct",
                root_id=None,             # 没有来源消息，不伪造一条被回复的
                user_id=None,
                is_proactive=True,
                bot_name=conv.bot_name,
                lane=lane,                # sink 不注入 header lane，必须显式带
                content=said,
                # 图走这一列，一个字符都没经过渲染那一步（见模块 docstring 上面
                # 那段：渲染是自由生成，混在正文里的图片引用必然被改写或丢掉）。
                # 传的是永久句柄，地址由投递侧在最靠近发送的那一刻现签。
                picture_file_names=file_names,
                status="success",
                is_last=True,
                full_content=said,
            )
        )
    except Exception:
        # **抛错 = 不知道，不是"确定没发出去"。** publisher confirm 超时、连接断在
        # 确认之前，broker 都可能已经收件了；而下游出站失败照样 ack、MQ 重投也没有
        # 发送级去重。所以这一格跟"崩在 emit 和收口之间"是同一件事：行停在
        # ``claimed``（:func:`unsettled_outbound` 捞得出来）、不留记忆、不重发。
        #
        # 不重发不是把这句话判死：``outbound_id`` 从 ``moment_id`` 派生，下一缝就是
        # 新的一条 —— 要不要再说一次是**她**的决定，不是系统替她按的重试按钮。
        # 所以这里也没有计数器、没有退避、没有超时阈值。
        logger.warning(
            "living mouth lane=%s persona=%s outbound=%s 交给投递时断了，"
            "结果未知（行停在 claimed，不重发）",
            lane,
            persona_id,
            outbound_id,
            exc_info=True,
        )
        # 如实说：不说"发失败了"（不知道），也不说"发出去了"（同样不知道），更不
        # 叫她再试一次（那是替她做决定）。
        return (
            f"「{said}」—— 这句话交出去的时候断了，不知道对方收没收到。"
            f"这一缝里不会再发一遍。"
        )

    # 落进她自己的记录里 —— 不落的话，她下一缝不知道自己说过这句话。
    # 位置缺失不拦：手机隔着设备，旁边的人本来就一个字都感知不到，所以位置算不算得
    # 出来都不影响谁听得见（跟当面说话不一样，那条必须有位置）。
    where = await current_whereabouts(lane=lane, persona_id=persona_id)
    happening_id = f"{OUTBOUND_HAPPENING_PREFIX}{outbound_id}"
    await record_happening(
        lane=lane,
        happening_id=happening_id,
        actor=persona_id,
        place=where.place if where is not None else "",
        kind=KIND_SPEECH,
        content=said,
        occurred_at=now,
        audience=(),
        medium=medium_for(conv.scope),
        channel_id=conv.channel_id,
    )
    note_recorded(happening_id)
    # 收口。崩在这之前的话留下一条 ``claimed``：交出去了、可能已经送到，而她可能不
    # 记得 —— :func:`unsettled_outbound` 把这种捞出来给人看，**不自动重发**。
    # 对账钟可能已经在这条链上追过一版，所以收口是"合到最新一版上"，见 :func:`_settle`。
    await _settle(claim, at=now)
    return f"发出去了：「{said}」"


MOUTH_TOOLS = [send_message]
