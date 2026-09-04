"""手机 —— 信封可感，内容要她去看。

**她每一缝拿到的只有信封**：有没有动静、谁在说、多密多快、跟她刚才干的事有没有牵连。
**内容要她调「看手机」才有。** 这不是省 token，是这个设计的骨头：拿起手机是一个动作，
有动作才有"已读"这回事；把内容白送进每一缝，"看手机"就成了摆设，而"她没看见"这个真
人每天都在经历的状态就再也不会发生。

**「看手机」是「打开这条会话」，不是「看未读」。** 她看到最近若干条**往来**：双向、
含她自己发的、含上一缝已经读过的上文。改之前那条查询同时带着三个条件（不是她发的、
没撤掉的、游标之后的），于是她眼前只有对方那一半 —— 决定说什么的那个模型从来没见过
一段完整的对话。实证（coe-living，2026-09-04）：她自己撤回了一句话，8 分钟后还在问
主人"你刚才到底发了啥"，因为那一缝她眼前只有孤零零一句未读，前面的来回全在游标之前，
而读过的消息不进任何持久记忆。真人点开一个会话看到的正是双向的最近若干条。

**入站一步不碰 MQ。** 每一缝直接查她未读的 ``common_message``。两个理由：不跟旧引擎抢
它那条入站队列；而且"投递只入信箱不唤醒"天然成立——消息本来就躺在库里，没有谁需要被
通知。这也是"chat 只有出口没有入口"的物理保证：**这个包里根本没有消费者**。

**游标是她的，不是钟的。**

  * 身边的事走提交序 ``seq``，没有已读这回事（在场就是感知到了，见
    :mod:`app.living.happening`）；
  * 手机走**持久游标**，因为已读是她的动作。

**"看手机"什么时候算数：这一缝跑完的时候。** 工具返回**不等于**她看见了——工具结果还
要进模型的上下文，这一缝才算真的把内容送到她眼前。所以 :func:`look_at_phone` 只把游标
攒进本缝的 ambient 状态，真正落库由 :func:`commit_glances` 跟 ``LifeMoment`` **在同一个
事务里**做。缝没跑完（进程被杀、模型那步炸了、收尾写库失败），一条都不算已读，她下一缝
原样再看到。**宁可重看，不可漏看。** 代价只有一条：同一缝里看两次得自己记着刚看过什么，
所以有效水位是"库里的"和"本缝攒的"取大。

**同毫秒不能被跳过：游标是复合的。** 只按 ``event_time > 水位`` 开窗的话，**整个那一
毫秒**都被排除——一条跟她刚读那条同毫秒、但晚一步落库的消息就此永久消失。所以水位是
``(event_time, common_message_id)``，按字典序推进；``common_message_id`` 在生产里是
uuidv7（按生成时刻单调），同毫秒里谁先谁后有确定答案。

**解不了的那半，如实说：** ``common_message`` 是既有表、没有提交序列（T1 给自己的记录
造了 ``seq`` 才解掉同类问题，这张表的写入方不是我们）。一条**晚落库、而 ``event_time``
比水位更早**的消息仍然会被永久跳过。复合游标只覆盖了"同毫秒"这一段边界，不是全部。

**窗口、未读、游标是三件事**（改之前它们是同一个查询条件的三个身份）：

  * **未读集合 U**：游标之后的、别人发的、没撤掉的那些。判据跟改之前逐字相同，
    信封和"谁在叫她"用的也是它。
  * **展示窗口 W**：这条会话上最近 :data:`PHONE_GLANCE_LIMIT` 条，**不看游标、不分谁
    发的**，含她自己撤掉的那条（留痕迹，见
    :data:`app.data.queries.messages._VISIBLE_WHEN_SHE_OPENS_IT`），不含
    别人撤掉的。
  * 「其中 N 条是新的」＝ ``|U ∩ W|``；「前面还有 K 条你没往回翻」＝ ``|U − W|``，也
    就是被挤出窗口的未读。
  * **游标推到 ``max(U)``，不是 ``max(W)``。** W 里最新那条可能是她自己发的、晚于任何
    未读；推到它身上会让之后乱序到达、时刻更早的消息被永久跳过，也会让措辞模型那侧
    （:func:`conversation_as_she_knows_it` 按游标开窗）把她根本没见过的消息当成"她已经
    知道的"。一条未读都没有时游标不动。

**挤出窗口的那些永久丢失。** 她一眼只看最后十来条，前面的不会补看，游标照样推到未读
里最新那条。这是设计不是 bug——真人"未读 47 条"就是先看最后五到十条，能自洽就到此为
止。给她做"补看队列"是替她做决定，而且真人根本没有那个东西。**但窗口里的东西不会
消失**：下一缝再点开还是那十条，跟真人再点一次看到同样的消息一致。

**看到读过的上文不配任何"防重复回应"的规则。** 不加计数器、不加去重、不加"这条你回过
了"的标记：每条消息都带着时刻，而且「其中 N 条是新的」直接告诉她哪些是新到的。她读得
出来，读不出来也是她的判断，不是需要被逻辑层消除的不确定性。真人翻聊天记录时看到的也
是全部历史，靠的同样是时间和上下文。

**公共层怎么读，不在这个模块里。** 这一层只回答"她此刻看到什么、她做了什么"；
``common_message`` / ``common_conversation`` / ``common_bot_presence`` /
``bot_config`` 的每一条查询都住在 :mod:`app.data.queries.messages` 和
:mod:`app.data.queries.persona`，这里只调函数。留在本模块的库操作只有她自己那两张
表（``PhoneRead`` 的游标、``Happening`` 的"上次在这儿开口"）。

**哪些会话在她手机上**：她自己的 bot 还在的那些（``common_bot_presence`` +
``bot_config.persona_id``），私聊和群同一条规则。用 presence 而不是"聊过天就算"，
是因为 bot 被移出群之后历史还在、但她既收不到也发不出去。判据见
:func:`app.data.queries.persona.find_conversations_with_persona_bot`。

**"这话是不是她说的"认 bot，不认 role。** 三姐妹本来就挂在同一个群里（prod
``common_bot_presence`` 实测：一个群里同时有 ayana / chinagi / chiwei），她们的出站
落进 ``common_message`` 全是 ``role='assistant'`` —— 长得一模一样。所以拿 ``role``
单独判会同时错两次：姐姐的话被整段排除出未读（她永远看不见同一个群里姐姐说了什么），
同时又被无条件塞进"她已经知道的"、还署上"你"。分得开这两者的是 ``bot_name``
（``bot_config`` 里 bot → persona 的映射），判据写在
:data:`app.data.queries.messages._SAID_BY_HER` 上。

**姐姐的群聊发言进未读，但一个字的召唤力都不多。** 群里不点名就是背景音，条数上限
照样管得着它（:meth:`Envelope.is_calling_you` 一个字没动）。同一个屋檐下的姐妹在群里
聊天，不该比陌生人更有召唤力 —— 真按"姐姐一说话就召唤"来，两个 agent 会在一个群里
互相把对方叫醒，永远停不下来。

**撤掉的那条不在会话里了。** 撤回不删 ``common_message`` 那一行（公共层是消息记录，
删行会打断历史），撤成功只在 ``recalled_at`` 上留个时刻。查询层每一处读那张表的地方
都带着 :data:`app.data.queries.messages._STILL_IN_THE_CONVERSATION` —— 少带一处，她就
在那个视角下还能看见一条自己明明撤掉了的话，然后接着它往下说，而对面早就看不到了。

**只有"打开会话"那一处例外，而且只对她自己撤掉的那条**：那儿留一条写明已经撤回的
痕迹并带上原话（:data:`app.data.queries.messages._VISIBLE_WHEN_SHE_OPENS_IT`）。理由
写在那个常量上。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, field_validator
from sqlalchemy import text

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.data.queries.messages import (
    find_conversation_window,
    find_messages_known_through,
    find_newest_unread_summons,
    find_unread_senders,
    find_unread_summary,
    search_conversations_by_name,
)
from app.data.queries.persona import (
    find_bot_names_for_persona,
    find_bot_user_ids_for_persona,
    find_conversations_with_persona_bot,
)
from app.data.session import get_session
from app.infra.cst_time import CST, to_cst_dated
from app.living.records import (
    MEDIUM_GROUP_CHAT,
    MEDIUM_PHONE,
    Happening,
    _require_aware,
)
from app.living.scope import FEATURE_GLANCES, moment_scope
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent

logger = logging.getLogger(__name__)

# 她点开一条会话看到多少条。**这是展示窗口的大小，不是未读的上限**——游标照样推到未读
# 里最新那条，被挤出窗口的那些是真的丢了，这就是设计本身（见模块 docstring）。十条约
# 等于真人点开一个会话一屏能看到的量。
PHONE_GLANCE_LIMIT = 10

# 信封里列多少条会话。手机上会话再多，一屏也就这些；超出的下一缝还在。
ENVELOPE_LIMIT = 8

# 信封里点几个发件人的名字。不是"最重要的几个"——是按最近说话的先后取前几个。
ENVELOPE_SENDER_LIMIT = 4


class PhoneRead(Data):
    """她在某一缝把某条会话看到了哪儿。

    自然键 ``(lane, persona_id, channel_id, read_through_message_id)``，**纯 append
    无版本链**：这条记的不是"游标现在是多少"这个可变状态，而是"她读到了这条为止"
    这件发生过的事。当前游标 = 这条轴上 ``read_through_ms`` 的最大值。

    选 append 而不是版本链 CAS，是因为同一个 persona 的缝本来就串行（
    :func:`app.living.serial.hold`），不存在并发推同一条游标的写者；而 append 白送
    一份可查的历史——"哪一缝读了哪条会话、读到哪条消息为止"事后能逐条查出来，版本链
    只留得下最后一版。

    **边界消息进自然键、``moment_id`` 不进**：重放同一次"看手机"落回同一行（同一条
    边界消息），而她在同一缝里第二次看到新来的消息时是**新的边界**、必须能推进。反过来
    把 moment_id 放进键，同一缝里的第二次看就成了重放、游标停住——她明明看过了，下一缝
    还得再看一遍。

    ``read_through_ms`` 是 ``common_message.event_time``（毫秒纪元）。用它而不是
    ``created_at``：event_time 是平台给的发生时刻，也是消息在会话里的排序依据。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    channel_id: Annotated[str, Key]   # common_conversation_id
    read_through_message_id: Annotated[str, Key]
    moment_id: str                    # 她哪一缝看的（可查，不进键）
    read_through_ms: int
    read_at: datetime

    class Meta:
        # 读侧唯一形状：这个人这条会话上的最大水位。
        indexes = (("lane", "persona_id", "channel_id"),)

    @field_validator("read_at")
    @classmethod
    def _aware_read_at(cls, v: datetime) -> datetime:
        return _require_aware("read_at", v)


_READ_TABLE = _table_name(PhoneRead)
_HAPPENING_TABLE = _table_name(Happening)


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
class Envelope:
    """一条会话的信封。**没有正文，一个字都没有。**

    ``named_you`` 是"有人点了你的名"这个客观事实（群里 @ 到她自己的 bot）；私聊没有
    这个概念，私聊本身就意味着有人在等她回。

    ``you_last_spoke_at`` 是"你上次在这儿开口是什么时候"。这是**事实**，不是我们替她
    算的权重——五分钟前刚在这个群说过话，和三天没说过话，这次动静对她不是一回事，但
    到底算不算数由她自己判。
    """

    channel_id: str
    scope: str
    title: str
    unread: int
    senders: tuple[str, ...]
    earliest: datetime
    latest: datetime
    named_you: bool
    you_last_spoke_at: datetime | None

    @property
    def is_calling_you(self) -> bool:
        """有人在叫她吗 —— 跟 :func:`newest_unread_summons` 同一条判据。

        私聊本身就意味着有人在等她回；群里点名是直接叫她。两条客观事实，没有第三条、
        没有分级。**这两条会话永远不会被信封的条数上限挤掉。**
        """
        return self.scope == "direct" or self.named_you


@dataclass(frozen=True)
class Summons:
    """一条**在叫她**的消息：私聊来的，或者群里点了她的名。

    "强"不是发送方标的：私聊之所以强，是因为私聊本身意味着有人在等她回；群里点名之所以
    强，是因为那是直接叫她。除此之外没有分级、没有权重表——那是替她做决定。
    """

    message_id: str
    channel_id: str
    at: datetime


# ---------------------------------------------------------------------------
# 她手机上有哪些会话
# ---------------------------------------------------------------------------

async def reachable_conversations(*, persona_id: str) -> list[Reachable]:
    """她手机上的全部会话。查不到就是查不到，不猜、不兜底。

    口径（presence + 她自己的 bot + 会话没归档）在
    :func:`app.data.queries.persona.find_conversations_with_persona_bot`。
    """
    rows = await find_conversations_with_persona_bot(persona_id)
    return [
        Reachable(
            channel_id=str(r["channel_id"]),
            scope=r["scope"],
            title=r["title"],
            channel=r["channel"],
            bot_name=r["bot_name"],
        )
        for r in rows
    ]


async def reachable_conversation(
    *, persona_id: str, channel_id: str
) -> Reachable | None:
    """她手机上的某一条会话；不在她手机上返回 ``None``。"""
    for conv in await reachable_conversations(persona_id=persona_id):
        if conv.channel_id == channel_id:
            return conv
    return None


# ---------------------------------------------------------------------------
# 读到哪了
# ---------------------------------------------------------------------------


# 复合水位：(event_time 毫秒, common_message_id)。从没看过 = 比任何消息都小。
NEVER_LOOKED: tuple[int, str] = (0, "")


async def read_through(
    *, lane: str, persona_id: str, channel_id: str
) -> tuple[int, str]:
    """她这条会话读到哪了：``(毫秒, 消息 id)``；从没看过返回 :data:`NEVER_LOOKED`。

    **是复合的，不只是时刻。** 只存毫秒的话，跟水位同毫秒、晚一步落库的那条消息会被
    整段跳过（``event_time > 水位`` 把整个那一毫秒排除掉），而且一句报错都没有。
    """
    async with get_session() as s:
        row = (
            await s.execute(
                text(
                    f"SELECT read_through_ms, read_through_message_id "
                    f"FROM {_READ_TABLE} "
                    f"WHERE lane = :lane AND persona_id = :persona_id "
                    f"AND channel_id = :channel_id "
                    f"ORDER BY read_through_ms DESC, read_through_message_id DESC "
                    f"LIMIT 1"
                ),
                {"lane": lane, "persona_id": persona_id, "channel_id": channel_id},
            )
        ).mappings().first()
    if row is None:
        return NEVER_LOOKED
    return int(row["read_through_ms"]), str(row["read_through_message_id"])


def _pending_cursor(channel_id: str) -> tuple[int, str] | None:
    """本缝已经看过、但还没落库的水位（同一缝里看两次时用得着）。

    不在一缝里（运维查一眼信封、离线盘点）就没有"本缝攒的"这回事，返回 ``None``。
    这里**不能**跟着 :func:`app.living.scope.moment_scope` 一起 fail-fast：那条是给
    工具体用的，工具体没绑 context 是 wiring bug；而信封本身在缝外面读是正当的。
    """
    try:
        ctx = get_context()
    except LookupError:
        return None
    best: tuple[int, str] | None = None
    for g in ctx.features.get(FEATURE_GLANCES, []):
        if g["channel_id"] != channel_id:
            continue
        here = (g["read_through_ms"], g["read_through_message_id"])
        if best is None or here > best:
            best = here
    return best


async def effective_cursor(
    *, lane: str, persona_id: str, channel_id: str
) -> tuple[int, str]:
    """她此刻**实际**读到哪了：库里的水位和本缝攒着的取大。"""
    landed = await read_through(
        lane=lane, persona_id=persona_id, channel_id=channel_id
    )
    pending = _pending_cursor(channel_id)
    return max(landed, pending) if pending is not None else landed


async def commit_glances(*, glances: list[dict], session: Any) -> int:
    """把本缝看过的手机落库。**由一缝的收尾调用，跟 ``LifeMoment`` 同一个事务。**

    只有这一步提交了，那些消息才算她看过。缝没跑到这里就一条都不算已读。
    """
    written = 0
    for g in glances:
        written += await insert_idempotent(PhoneRead(**g), session=session)
    return written


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------

# 她上次在这条会话上开口是什么时候。读的是她自己的 Happening（嘴落下的那条），
# 不是 common_message —— 出站消息要等 chat-response-worker 异步落库，按它算会
# 把刚说完的话算成"从没说过"。
#
# 这是本模块里唯一一条还自己拿 session 的查询，因为它读的是 living 自己那张表。
_LAST_SPOKE_SQL = f"""
SELECT MAX(occurred_at) FROM {_HAPPENING_TABLE}
 WHERE lane = :lane AND actor = :persona_id AND channel_id = :channel_id
"""


async def _last_spoke_at(
    *, lane: str, persona_id: str, channel_id: str
) -> datetime | None:
    """她上次在这条会话上开口的时刻；从没说过返回 ``None``。"""
    async with get_session() as s:
        return (
            await s.execute(
                text(_LAST_SPOKE_SQL),
                {
                    "lane": lane,
                    "persona_id": persona_id,
                    "channel_id": channel_id,
                },
            )
        ).scalar()


def _instant(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=CST)


async def envelopes_for(*, lane: str, persona_id: str) -> list[Envelope]:
    """她此刻手机上有动静的那些会话。**在叫她的那些一条都不会少。**

    条数上限只管**没在叫她的**那批（群里的背景噪音，本来就无上限）。这个区别不是
    美观问题：信封截掉的是"她知不知道有这回事"，比"她看多少条内容"严重一个量级——
    被挤出去的那条会话，她连它存在都不知道，也就永远不会想起去看。一屋子群在刷屏
    的时候，正在等她回话的那条私聊必须还在眼前，**谁值得先回是她判，不是这里判**。
    """
    bot_uids = await find_bot_user_ids_for_persona(persona_id)
    own_bots = await find_bot_names_for_persona(persona_id)
    out: list[Envelope] = []
    for conv in await reachable_conversations(persona_id=persona_id):
        after_ms, after_id = await effective_cursor(
            lane=lane, persona_id=persona_id, channel_id=conv.channel_id
        )
        row = await find_unread_summary(
            channel_id=conv.channel_id,
            after_ms=after_ms,
            after_id=after_id,
            bot_user_ids=bot_uids,
            own_bots=own_bots,
        )
        if row is None or not row["unread"]:
            continue
        senders = await find_unread_senders(
            channel_id=conv.channel_id,
            after_ms=after_ms,
            after_id=after_id,
            own_bots=own_bots,
            limit=ENVELOPE_SENDER_LIMIT,
        )
        spoke = await _last_spoke_at(
            lane=lane, persona_id=persona_id, channel_id=conv.channel_id
        )
        out.append(
            Envelope(
                channel_id=conv.channel_id,
                scope=conv.scope,
                title=conv.title,
                unread=int(row["unread"]),
                senders=tuple(r["who"] for r in senders),
                earliest=_instant(int(row["earliest"])),
                latest=_instant(int(row["latest"])),
                named_you=bool(row["named_you"]),
                you_last_spoke_at=spoke,
            )
        )
    out.sort(key=lambda e: e.latest, reverse=True)
    calling = [e for e in out if e.is_calling_you]
    rest = [e for e in out if not e.is_calling_you]
    return calling + rest[: max(0, ENVELOPE_LIMIT - len(calling))]


def _clock(moment: datetime, *, now: datetime) -> str:
    """一个时刻的样子：同一个 CST 日历日给 ``HH:MM CST``，跨天补上 ``MM-DD``。

    **不能给裸时分。** 信箱既没有时间窗也没有 TTL——她睡着的时候消息照堆、一条都不算
    已读（见模块 docstring），所以昨晚积压的未读原样进这一缝。而 ``23:50`` 这个形状
    昨晚和今晚长得一模一样，她无从分辨那是几分钟前还是一整夜前。线上炸过一次
    （2026-08-03：中午 13:18 往群里发「大半夜的发什么疯、赶紧滚去睡觉」）。
    ``you_last_spoke_at`` 更是如此：这条事实存在的全部意义就是让她分得清"五分钟前刚
    说过"和"昨晚说过"。

    ``now`` 从调用方传进来（一缝的时间锚），**不在这里现取**：一缝一个 now 是这套引擎
    的地基（:mod:`app.living.anchor`），渲染层自己读钟会让同一缝的输入跟它的身份对不上。

    走 :func:`app.infra.cst_time.to_cst_dated` 而不是自己比日期：跨天判定要按 CST 日历
    日（不是 UTC、也不是"差了 24 小时"），这条逻辑只该有一处定义。它收的是原始时间串，
    所以这里把已经是 ``datetime`` 的值 ``isoformat()`` 回去 —— aware ISO 正是它认的三
    种格式之一，往返是精确的。
    """
    return to_cst_dated(moment.isoformat(), now=now, seconds=False)


def render_envelopes(envelopes: list[Envelope], *, now: datetime) -> str:
    """把信封摆成她读得懂的样子。**正文一个字都不在这里。**

    ``channel_id`` 紧跟在会话名后面，不甩到行尾。实测（coe-living，2026-08-31
    20:21）她拿人名去调 :func:`look_at_phone`、被顶了回来：名字在最显眼处、地址挂
    在七八个属性之后，而在她眼里那条私聊本来就叫那个名字，uuid 是工程产物。工具
    描述里写「照抄别自己编」是在跟这个错位对抗，治不了。名字和地址绑成一个东西，
    她要指哪条会话时才有个完整的可指之物。
    """
    if not envelopes:
        return "手机上：（没动静）"
    lines = ["手机上（想知道说了什么，得自己拿起来看）："]
    for e in envelopes:
        where = "私聊" if e.scope == "direct" else "群"
        bits = [
            f"- {where}「{e.title}」channel_id={e.channel_id} · "
            f"{e.unread} 条没看",
            "、".join(e.senders) or "某人",
            f"{_clock(e.earliest, now=now)}–{_clock(e.latest, now=now)}",
        ]
        if e.named_you:
            bits.append("有人点了你的名")
        bits.append(
            f"你上次在这儿开口是 {_clock(e.you_last_spoke_at, now=now)}"
            if e.you_last_spoke_at is not None
            else "你还没在这儿说过话"
        )
        lines.append(" · ".join(bits))
    return "\n".join(lines)


async def phone_envelope(*, lane: str, persona_id: str, now: datetime) -> str:
    """她这一缝手机上的信封，一整段文本。

    ``now`` 是这一缝的时间锚，信封上每个时刻都拿它判跨没跨天（见 :func:`_clock`）。
    """
    return render_envelopes(
        await envelopes_for(lane=lane, persona_id=persona_id), now=now
    )


# ---------------------------------------------------------------------------
# 谁在叫她（提前一缝的输入；判断在 app.living.nudge）
# ---------------------------------------------------------------------------


async def newest_unread_summons(
    *, lane: str, persona_id: str
) -> Summons | None:
    """还没被她看过、而且是在叫她的那条消息里最新的一条；没有返回 ``None``。

    "在叫她"= 私聊来的任意一条，或者群里点了她名字的那条，除此之外没有分级 ——
    判据在 :func:`app.data.queries.messages.find_newest_unread_summons`。
    """
    bot_uids = await find_bot_user_ids_for_persona(persona_id)
    own_bots = await find_bot_names_for_persona(persona_id)
    newest: Summons | None = None
    for conv in await reachable_conversations(persona_id=persona_id):
        after_ms, after_id = await read_through(
            lane=lane, persona_id=persona_id, channel_id=conv.channel_id
        )
        row = await find_newest_unread_summons(
            channel_id=conv.channel_id,
            after_ms=after_ms,
            after_id=after_id,
            is_direct=conv.scope == "direct",
            bot_user_ids=bot_uids,
            own_bots=own_bots,
        )
        if row is None:
            continue
        found = Summons(
            message_id=str(row["message_id"]),
            channel_id=conv.channel_id,
            at=_instant(int(row["at_ms"])),
        )
        if newest is None or found.at > newest.at:
            newest = found
    return newest


# ---------------------------------------------------------------------------
# 看手机
# ---------------------------------------------------------------------------

# 附件在她眼里叫什么。这里是全项目唯一一份 —— 聊天那条路曾经有过自己的一份
# （``app.chat.content_parser``），它随那条路一起删了。
#
# 名字能带就带：她要判断"这东西我看没看过、值不值得打开"，靠的是文件名，不是
# ``file_v3_...`` 那串 key。图片、表情包、语音本来就没有文件名，自然落回没名字那档。
_ATTACHMENT_LABEL = {
    "image": "图片",
    "sticker": "表情包",
    "audio": "语音",
    "file": "文件",
    "media": "视频",
}


def _body_of(row) -> str:
    """一条消息的正文。**以 items 为准，``content_text`` 只是兜底。**

    反过来（先信 ``content_text``）她就永远看不出附件是什么东西：那一列不是正文，
    是投影层拼给人扫一眼的摘要 —— 文本项原样，其余每一项一律拼成字面的 ``[kind]``
    （lark-service ``inbound-projection.ts`` 的 ``summarize``、channel-server
    ``common-projector.ts`` 的 ``textProjection``）。所以一条文件消息的
    ``content_text`` 就是 ``"[file]"``，非空、于是 items 里的 ``meta.file_name``
    一眼都没被看过。实测（coe-living，2026-09-02 22:27）她看到「某某：[file]」，
    只知道有个东西、不知道是什么，回了一句「发来看看」—— 那文件早就发过来了。
    图文混排更狠：文字非空就直接返回，附件在她眼里整个不存在。

    ``content_text`` 仍然留着当兜底 —— ``content`` 不是数组、或者一条 item 都渲染
    不出东西的历史行，还有这一列可以指望。
    """
    items = row["content"]
    if isinstance(items, str):
        items = json.loads(items)
    if not isinstance(items, list):
        # jsonb 里存的不保证是数组（同一条防线在
        # :data:`app.data.queries.messages._SENT_FILES_SQL` 的
        # ``jsonb_typeof`` 那儿）。认不出形状就整条交给 ``content_text``。
        items = []
    parts: list[str] = []
    for item in items:
        # 两套形状都得认：现在的写入方用 ``kind``，少数历史行用 ``type``/``value``。
        kind = item.get("type") or item.get("kind")
        value = item.get("value", item.get("text", ""))
        if kind in ("text", "unsupported"):
            # ``unsupported`` 带的是投影层给人看的中文占位串（``[合并转发]``、
            # ``[分享个人名片]``），不是类型名 —— 当成不认识的 kind 处理，她看到的
            # 就退成「[unsupported]」，比什么都不改还糟。
            parts.append(str(value))
        elif kind == "mention":
            parts.append(f"@{value}")
        elif kind in _ATTACHMENT_LABEL:
            name = (item.get("meta") or {}).get("file_name")
            label = _ATTACHMENT_LABEL[kind]
            parts.append(f"[{label}: {name}]" if name else f"[{label}]")
        elif kind:
            # 认不出的 kind 照样留个印子：一条消息静悄悄地少掉，比看到一个陌生
            # 类型名更难查。
            parts.append(f"[{kind}]")
    body = "".join(parts).strip()
    if body:
        return body
    return (row["content_text"] or "").strip() or "（没有内容）"


def _take_back_handle(row) -> str | None:
    """这一行她能拿去撤回的那个编号；撤不了的行返回 ``None``。

    三个条件缺一不可：**是她说的**（姐姐主动发的行同样有这一列，但她撤不动姐姐的）、
    **还没撤掉**（已经撤回的再撤一次只会撤了个空，那时留着编号等于同时说"这条撤回了"
    和"拿这串去撤它"）、**有这一列**（她回复别人的消息走另一条链，那条链不写它）。

    印出去的写法是 32 位无短横的 hex —— 跟 :func:`app.living.snapshot._own_line` 在
    「你刚做过、说过」那段里印的**必须逐字一致**，否则她会以为那是两种编号。库里这一
    列是 uuid 类型，两种写法之间的相等关系由两侧共读的成对向量钉住
    （``contracts/proactive-message-id.json`` 的 ``outbound_id_vector``）。
    """
    if not row["said_by_you"] or row["recalled_at"] is not None:
        return None
    outbound_id = row["outbound_id"]
    if outbound_id is None:
        return None
    return uuid.UUID(str(outbound_id)).hex


def _one_message(row, *, now: datetime) -> str:
    """一条消息渲染成一行。

    署名认 ``bot_name``（``said_by_you`` 那一列算好的），跟她开口前读的那段尾巴
    （:func:`conversation_as_she_knows_it`）用同一条判据：同群的姐姐也是
    ``role='assistant'``，按 role 署名就是把姐姐的话标成她自己说的。

    撤回那句话只说得出口的那件事：**这条消息已经撤回了**。不说"你撤回了"——群主和
    管理员也撤得掉她的消息，而撤回在库里只有一个时刻、没有操作者。"这行是她发的 +
    有撤回时刻"是可证明的，"是她自己按的撤回"不是。
    """
    who = "你" if row["said_by_you"] else row["who"]
    line = (
        f"{_clock(_instant(int(row['at_ms'])), now=now)} "
        f"{who}：{_body_of(row)}"
    )
    if row["recalled_at"] is not None:
        return line + "［这条消息已经撤回了，对面看不到它］"
    handle = _take_back_handle(row)
    return line + (f"［{handle}］" if handle is not None else "")


def _glance_text(
    *, title: str, rows: list, fresh: int, older_unread: int, now: datetime
) -> str:
    """她点开这条会话看到的东西。

    ``fresh``（``|U ∩ W|``）一定说，**零也说**：窗口里有她上一缝已经读过的上文，哪些
    是新到的只有这个数说得清。``older_unread``（``|U − W|``）是被挤出窗口的未读，它们
    不会在别处被补回来。
    """
    lines = [_one_message(r, now=now) for r in reversed(rows)]
    head = f"「{title}」（其中 {fresh} 条是新的"
    if older_unread > 0:
        head += f"，前面还有 {older_unread} 条你没往回翻，就这么过去了"
    return head + "）\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 按名字找回一条会话的地址
# ---------------------------------------------------------------------------


@tool
@tool_error("找会话失败")
async def look_up_contact(
    name: Annotated[
        str, Field(description="你要找的人或群的名字，记得多少写多少")
    ],
) -> str:
    """报一个名字，找回他在你手机上的那条会话。

    手机上每一缝给你的信封只列**有动静的**那些。一条会话你读完了、对方没再说话，
    它就不在信封上了——想主动找回某个人的时候用这只手。

    名字对得上的会话都列出来，各带一串 channel_id，拿它调 look_at_phone 或
    send_message。哪一条是你要找的那个人，你自己认。

    Args:
        name: 你要找的人或群的名字。

    Returns:
        名字对得上的会话，每条带 channel_id；一条都没有时如实说明。
    """
    _, now, persona_id, _ = moment_scope()
    wanted = name.strip()
    if not wanted:
        raise ValueError("name 不能是空的：写一个你要找的名字。")

    # 会话集合从 reachable_conversations 来，跟信封那条路同一个来源 —— 这只手能搜
    # 到的严格等于她能收到的。
    rows = await search_conversations_by_name(
        conversations=[
            {"channel_id": c.channel_id, "scope": c.scope, "title": c.title}
            for c in await reachable_conversations(persona_id=persona_id)
        ],
        name_like=f"%{wanted}%",
        own_bots=await find_bot_names_for_persona(persona_id),
    )

    if not rows:
        return f"手机上没有叫「{wanted}」的人或群。"

    lines = [f"手机上叫「{wanted}」的："]
    for r in rows:
        where = "私聊" if r["scope"] == "direct" else "群"
        # 私聊的会话标题多半是空的，这时用对得上的那个人名当它的名字——在她眼里
        # 那条私聊本来就叫那个人。地址紧跟名字，理由见 render_envelopes。
        matched = list(r["matched"] or [])
        label = r["title"] or "、".join(matched) or "（没名字）"
        bits = [f"- {where}「{label}」channel_id={r['channel_id']}"]
        if matched and r["title"]:
            bits.append("、".join(matched) + " 在里面说过话")
        if r["latest"] is not None:
            bits.append(f"最近一次 {_clock(_instant(int(r['latest'])), now=now)}")
        lines.append(" · ".join(bits))
    return "\n".join(lines)


@tool
@tool_error("看手机失败")
async def look_at_phone(
    channel_id: Annotated[
        str, Field(description="哪条会话，用信封上那串 channel_id")
    ],
) -> str:
    """拿起手机打开一条会话，看最近说了些什么。

    信封只告诉你有动静、谁、多少条。**内容要调这个才有。**

    你看到的是这条会话上最近的十来条往来——**双向的**，你自己发的也在里面，上一次
    看过的上文也还在。其中哪几条是新到的会单独告诉你。再往前那些不会再回来，就像你
    真的划开一个未读很多的会话，扫一眼最后几条，前面的就那么过去了。

    你自己发的、还撤得回来的那些，行末带着一串编号——要撤哪一条就照抄那串。你回复
    别人的消息和别人发的消息没有编号，那是因为它们本来就撤不了。已经撤回的那条会
    留在原处、写明它撤掉了，原话也还在，但对面已经看不到它了。

    你没调它的时候，消息照堆着、一条都不算你看过。

    Args:
        channel_id: 哪条会话（信封上那串）。

    Returns:
        这条会话最近的十来条往来，以及其中几条是新到的。
    """
    lane, now, persona_id, moment_id = moment_scope()
    conv = await reachable_conversation(
        persona_id=persona_id, channel_id=channel_id.strip()
    )
    if conv is None:
        raise ValueError(
            f"{channel_id!r} 不是你手机上的会话 —— 用信封上那串 channel_id，"
            f"照抄，别自己编。"
        )

    after_ms, after_id = await effective_cursor(
        lane=lane, persona_id=persona_id, channel_id=conv.channel_id
    )
    # 一条语句同时给出窗口、未读总数和 ``max(U)``，理由写在
    # :data:`app.data.queries.messages._OPEN_CONVERSATION_SQL` 上：两条语句就是两个
    # 快照，中间提交的那条消息会被永久跳过。三列在每一行上都一样，取第一行即可。
    rows = await find_conversation_window(
        channel_id=conv.channel_id,
        after_ms=after_ms,
        after_id=after_id,
        own_bots=await find_bot_names_for_persona(persona_id),
        limit=PHONE_GLANCE_LIMIT,
    )

    if not rows:
        # 窗口为空 ⟹ 未读也为空（推理见
        # :data:`app.data.queries.messages._OPEN_CONVERSATION_SQL`），所以这里
        # 直接返回、游标不动是完备的，不是漏了一种情况。
        return f"「{conv.title}」上一条消息都没有。"

    fresh = sum(1 for r in rows if r["is_unread"])
    unread_total = int(rows[0]["unread_total"])
    seen = _glance_text(
        title=conv.title,
        rows=list(rows),
        fresh=fresh,
        older_unread=unread_total - fresh,
        now=now,
    )

    # 水位推到**未读里最新的那条**（``max(U)``），不是窗口里最新那条：窗口里最新那条
    # 可能是她自己发的、晚于任何未读，推到它身上会让之后乱序到达、时刻更早的消息被
    # 永久跳过，也会让措辞模型那侧把她根本没见过的消息当成"她已经知道的"。被挤出窗口
    # 的未读就此丢了 —— 这正是这条设计要的行为，不是漏。一条未读都没有时游标不动。
    #
    # **但不在这儿落库。** 工具返回不等于她看见了——结果还要进模型的上下文，这一缝才
    # 算真的把内容送到她眼前。当场提交的话，崩在中间就是"已读了但内容从没进过她的
    # 上下文"，那几条永久消失且一句报错都没有。所以攒进本缝状态，由 ``run_moment``
    # 的收尾跟 ``LifeMoment`` 一个事务落库（:func:`commit_glances`）。
    newest_unread_id = rows[0]["newest_unread_id"]
    if newest_unread_id is not None:
        get_context().features.setdefault(FEATURE_GLANCES, []).append(
            {
                "lane": lane,
                "persona_id": persona_id,
                "channel_id": conv.channel_id,
                "read_through_message_id": str(newest_unread_id),
                "moment_id": moment_id,
                "read_through_ms": int(rows[0]["newest_unread_ms"]),
                "read_at": now,
            }
        )
    return seen


PHONE_TOOLS = [look_at_phone, look_up_contact]


# ---------------------------------------------------------------------------
# 她**已经知道**的那部分会话（嘴渲染措辞时能看的全部）
# ---------------------------------------------------------------------------

# 渲染措辞时回看多少条。够她接住上下文，又不至于把一整天的会话灌进去。
KNOWN_TAIL_LIMIT = 20


async def conversation_as_she_knows_it(
    *,
    lane: str,
    persona_id: str,
    channel_id: str,
    now: datetime,
    limit: int = KNOWN_TAIL_LIMIT,
) -> str:
    """这条会话上她已经知道的那一段，按时间升序，一条一行。

    没看过的消息不在里面 —— 边界就是游标：她看过的（``event_time <=`` 水位）+
    **她自己**发出去的，判据在
    :func:`app.data.queries.messages.find_messages_known_through`。嘴是用来把她的
    意思说成人话的，不是用来替她读消息的；给它未读内容，"内容要她去看"这条线当场
    就漏了。她从没看过、也没说过话的会话，返回一句如实的空。

    署名认 ``bot_name``：只有**她自己**那些 bot 发的才写"你"。同一个群里姐姐也是
    ``role='assistant'``，按 role 署名就是把姐姐的话标成她自己说的。

    ``now`` 是**这一缝的时间锚**（``moment_scope()`` 的第二个返回值），不是现取的钟——
    跟信封、快照同一个来源，否则同一轮里两处时间会差开。它只用来判每一行要不要背
    ``MM-DD``：这 :data:`KNOWN_TAIL_LIMIT` 条**没有时间窗**，昨晚说过的话原样躺在里面，
    而这一段是她开口前读的最后一样东西（:func:`app.living.mouth.send_message`）。裸时分
    下「23:50 我先睡了」和五分钟前刚说的长得一个样，她会接着一段其实已经隔夜的对话往
    下说。
    """
    cursor_ms, cursor_id = await effective_cursor(
        lane=lane, persona_id=persona_id, channel_id=channel_id
    )
    rows = await find_messages_known_through(
        channel_id=channel_id,
        cursor_ms=cursor_ms,
        cursor_id=cursor_id,
        own_bots=await find_bot_names_for_persona(persona_id),
        limit=limit,
    )
    if not rows:
        return "（这条会话上你还什么都没看过、也没说过）"
    lines = []
    for r in reversed(rows):
        who = "你" if r["said_by_you"] else r["who"]
        at = _clock(_instant(int(r["at_ms"])), now=now)
        lines.append(f"{at} {who}：{_body_of(r)}")
    return "\n".join(lines)

# medium 由会话本身决定：私聊是手机上一对一，群是群里说话。两者都隔着设备，所以坐在
# 她旁边的姐姐一个字都看不见（裁剪在 app.living.happening 的读取路径里）。
_MEDIUM_BY_SCOPE = {"direct": MEDIUM_PHONE, "group": MEDIUM_GROUP_CHAT}


def medium_for(scope: str) -> str:
    """这条会话上说的话，落成哪一档 medium。"""
    return _MEDIUM_BY_SCOPE.get(scope, MEDIUM_GROUP_CHAT)
