"""手机 —— 信封可感，内容要她去看。

**她每一缝拿到的只有信封**：有没有动静、谁在说、多密多快、跟她刚才干的事有没有牵连。
**内容要她调「看手机」才有。** 这不是省 token，是这个设计的骨头：拿起手机是一个动作，
有动作才有"已读"这回事；把内容白送进每一缝，"看手机"就成了摆设，而"她没看见"这个真
人每天都在经历的状态就再也不会发生。

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

**跳过的永久丢失**（:data:`PHONE_GLANCE_LIMIT`）。她一眼只看最后几条，中间那些不会
补看，游标照样推到最新。这是设计不是 bug——真人"未读 47 条"就是先看最后五到十条，
能自洽就到此为止。给她做"补看队列"是替她做决定，而且真人根本没有那个东西。

**哪些会话在她手机上**：她自己的 bot 还在的那些（``common_bot_presence`` +
``bot_config.persona_id``），私聊和群同一条规则。用 presence 而不是"聊过天就算"，
是因为 bot 被移出群之后历史还在、但她既收不到也发不出去。

**"这话是不是她说的"认 bot，不认 role。** 三姐妹本来就挂在同一个群里（prod
``common_bot_presence`` 实测：一个群里同时有 ayana / chinagi / chiwei），她们的出站
落进 ``common_message`` 全是 ``role='assistant'`` —— 长得一模一样。所以拿 ``role``
单独判会同时错两次：姐姐的话被整段排除出未读（她永远看不见同一个群里姐姐说了什么），
同时又被无条件塞进"她已经知道的"、还署上"你"。分得开这两者的是 ``bot_name``
（``bot_config`` 里 bot → persona 的映射），见 :data:`_SAID_BY_HER`。

**姐姐的群聊发言进未读，但一个字的召唤力都不多。** 群里不点名就是背景音，条数上限
照样管得着它（:meth:`Envelope.is_calling_you` 一个字没动）。同一个屋檐下的姐妹在群里
聊天，不该比陌生人更有召唤力 —— 真按"姐姐一说话就召唤"来，两个 agent 会在一个群里
互相把对方叫醒，永远停不下来。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, field_validator
from sqlalchemy import text

from app.agent.runtime_context import get_context
from app.agent.tooling import tool
from app.agent.tools._common import tool_error
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

# 她一眼看多少条。**不是截断上下文**——游标照样推到最新，中间那些是真的丢了，这就是
# 设计本身（见模块 docstring）。十条约等于真人扫一眼未读的量。
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

_REACHABLE_SQL = """
SELECT cc.common_conversation_id AS channel_id,
       cc.scope                  AS scope,
       COALESCE(cc.display_name, '') AS title,
       cc.channel                AS channel,
       MIN(bc.bot_name)          AS bot_name
  FROM common_bot_presence bp
  JOIN bot_config bc
    ON bc.bot_name = bp.bot_name
   AND bc.persona_id = :persona_id
   AND bc.is_active = true
  JOIN common_conversation cc
    ON cc.common_conversation_id = bp.common_conversation_id
   AND cc.is_active = true
 WHERE bp.is_active = true
 GROUP BY cc.common_conversation_id, cc.scope, cc.display_name, cc.channel
"""


async def reachable_conversations(*, persona_id: str) -> list[Reachable]:
    """她手机上的全部会话。查不到就是查不到，不猜、不兜底。"""
    async with get_session() as s:
        rows = (
            await s.execute(text(_REACHABLE_SQL), {"persona_id": persona_id})
        ).mappings().all()
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
# 这话是谁说的
# ---------------------------------------------------------------------------

# **``role`` 说不出"是谁说的"。** ``role='assistant'`` 只意味着"某个 bot 发的"，而三
# 姐妹挂在同一个群里，她们的出站在这张表里长得一模一样。唯一分得开的是 ``bot_name``
# —— ``bot_config`` 里 bot → persona 那条映射（出站的写入方两边都填了这一列：
# ``apps/lark-service/src/lark/outbound/deliver.ts`` 和 channel-server 的 QQ 投影）。
#
# ``bot_name`` 为空的历史行按"她自己说的"算。认不出是谁的 bot 时宁可少一条未读，也
# 不能把她自己的话摆成别人在说 —— 那正是前几次栽过的病（她把自己的回声当成别人在
# 说话）。反过来漏一条只是少看见一句，下一条来了照样看得见。
_SAID_BY_HER = (
    "(cm.role = 'assistant'"
    " AND (cm.bot_name IS NULL"
    "      OR cm.bot_name = ANY(CAST(:own_bots AS text[]))))"
)


async def _own_bot_names(*, persona_id: str) -> list[str]:
    """她那些 bot 的名字 —— "这句话是不是她说的"的全部依据。

    是列表不是单值：一个 persona 线上就挂着好几个 bot（正式那个和 dev 那个指向同一
    个人）。查不到就是空列表，那意味着她根本没有 bot ——
    :func:`reachable_conversations` 同样返回空，她手机上一条会话都没有，不会有
    "把所有人的话都当成别人说的"这种半截状态。
    """
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT bot_name FROM bot_config "
                    "WHERE persona_id = :pid AND is_active = true"
                ),
                {"pid": persona_id},
            )
        ).scalars().all()
    return [str(r) for r in rows]


# ---------------------------------------------------------------------------
# 信封
# ---------------------------------------------------------------------------

# 未读 = 不是她自己说的、event_time 在她这条会话的水位之后。她自己发出去的那些不算
# 未读——那是她说的话，落在 Happening 里。**姐姐说的算**：同一个群里的动静她本该
# 感知到。
_UNREAD_SUMMARY_SQL = f"""
SELECT COUNT(*)                          AS unread,
       MIN(cm.event_time)                AS earliest,
       MAX(cm.event_time)                AS latest,
       BOOL_OR(EXISTS (
           SELECT 1 FROM jsonb_array_elements(
               CASE WHEN jsonb_typeof(cm.content) = 'array'
                    THEN cm.content ELSE '[]'::jsonb END
           ) AS it
            WHERE it->>'type' = 'mention'
              AND it->'meta'->>'bot_common_user_id'
                  = ANY(CAST(:bot_user_ids AS text[]))
       ))                                AS named_you
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND NOT {_SAID_BY_HER}
   AND (cm.event_time, CAST(cm.common_message_id AS text))
       > (:after_ms, :after_id)
"""

_UNREAD_SENDERS_SQL = f"""
SELECT COALESCE(cm.sender_display_name, '某人') AS who,
       MAX(cm.event_time)                       AS latest
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND NOT {_SAID_BY_HER}
   AND (cm.event_time, CAST(cm.common_message_id AS text))
       > (:after_ms, :after_id)
 GROUP BY 1
 ORDER BY 2 DESC
 LIMIT :limit
"""

# 她上次在这条会话上开口是什么时候。读的是她自己的 Happening（嘴落下的那条），
# 不是 common_message —— 出站消息要等 chat-response-worker 异步落库，按它算会
# 把刚说完的话算成"从没说过"。
_LAST_SPOKE_SQL = f"""
SELECT MAX(occurred_at) FROM {_HAPPENING_TABLE}
 WHERE lane = :lane AND actor = :persona_id AND channel_id = :channel_id
"""


async def _bot_user_ids(*, persona_id: str) -> list[str]:
    """她自己那些 bot 的 ``common_user_id`` —— 群里"点的是不是她"的全部依据。

    群消息里的 @ 在 ``common_message.content`` 里是一条 mention item，meta 上带着
    被点那个 bot 的 common_user_id（channel-server 写入时就解析好了）。所以"被点名"
    是**库里的客观事实**，不需要模型判断，也不需要从 MQ 拿 ``persona_ids``。
    """
    async with get_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT common_user_id FROM bot_config "
                    "WHERE persona_id = :pid AND is_active = true "
                    "AND common_user_id IS NOT NULL"
                ),
                {"pid": persona_id},
            )
        ).scalars().all()
    return [str(r) for r in rows]


def _instant(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=CST)


async def envelopes_for(*, lane: str, persona_id: str) -> list[Envelope]:
    """她此刻手机上有动静的那些会话。**在叫她的那些一条都不会少。**

    条数上限只管**没在叫她的**那批（群里的背景噪音，本来就无上限）。这个区别不是
    美观问题：信封截掉的是"她知不知道有这回事"，比"她看多少条内容"严重一个量级——
    被挤出去的那条会话，她连它存在都不知道，也就永远不会想起去看。一屋子群在刷屏
    的时候，正在等她回话的那条私聊必须还在眼前，**谁值得先回是她判，不是这里判**。
    """
    bot_uids = await _bot_user_ids(persona_id=persona_id)
    own_bots = await _own_bot_names(persona_id=persona_id)
    out: list[Envelope] = []
    for conv in await reachable_conversations(persona_id=persona_id):
        after_ms, after_id = await effective_cursor(
            lane=lane, persona_id=persona_id, channel_id=conv.channel_id
        )
        params = {
            "channel_id": conv.channel_id,
            "after_ms": after_ms,
            "after_id": after_id,
            "bot_user_ids": bot_uids,
            "own_bots": own_bots,
        }
        async with get_session() as s:
            row = (
                await s.execute(text(_UNREAD_SUMMARY_SQL), params)
            ).mappings().first()
            if row is None or not row["unread"]:
                continue
            senders = (
                await s.execute(
                    text(_UNREAD_SENDERS_SQL),
                    {**params, "limit": ENVELOPE_SENDER_LIMIT},
                )
            ).mappings().all()
            spoke = (
                await s.execute(
                    text(_LAST_SPOKE_SQL),
                    {
                        "lane": lane,
                        "persona_id": persona_id,
                        "channel_id": conv.channel_id,
                    },
                )
            ).scalar()
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

# 在叫她 = 私聊来的任意一条，或者群里点了她名字的那条。除此之外没有分级。
#
# **姐姐在群里说话不在这里面。** 她的话进未读、进信封（同一个群里的动静她本该感知
# 到），但群里不点名就是背景音 —— 不点名却算召唤的话，两个 agent 在一个群里会互相
# 把对方叫醒，永远停不下来。点名了就跟真人点名一样算，同一条判据，不多不少。
_SUMMONS_SQL = f"""
SELECT cm.common_message_id AS message_id,
       cm.event_time        AS at_ms
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND NOT {_SAID_BY_HER}
   AND (cm.event_time, CAST(cm.common_message_id AS text))
       > (:after_ms, :after_id)
   AND (
        :is_direct
        OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(cm.content) = 'array'
                     THEN cm.content ELSE '[]'::jsonb END
            ) AS it
             WHERE it->>'type' = 'mention'
               AND it->'meta'->>'bot_common_user_id'
                   = ANY(CAST(:bot_user_ids AS text[]))
        )
   )
 ORDER BY cm.event_time DESC, cm.common_message_id DESC
 LIMIT 1
"""


async def newest_unread_summons(
    *, lane: str, persona_id: str
) -> Summons | None:
    """还没被她看过、而且是在叫她的那条消息里最新的一条；没有返回 ``None``。"""
    bot_uids = await _bot_user_ids(persona_id=persona_id)
    own_bots = await _own_bot_names(persona_id=persona_id)
    newest: Summons | None = None
    for conv in await reachable_conversations(persona_id=persona_id):
        after_ms, after_id = await read_through(
            lane=lane, persona_id=persona_id, channel_id=conv.channel_id
        )
        async with get_session() as s:
            row = (
                await s.execute(
                    text(_SUMMONS_SQL),
                    {
                        "channel_id": conv.channel_id,
                        "after_ms": after_ms,
                        "after_id": after_id,
                        "is_direct": conv.scope == "direct",
                        "bot_user_ids": bot_uids,
                        "own_bots": own_bots,
                    },
                )
            ).mappings().first()
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

_GLANCE_SQL = f"""
SELECT cm.common_message_id AS message_id,
       COALESCE(cm.sender_display_name, '某人') AS who,
       cm.content           AS content,
       cm.content_text      AS content_text,
       cm.event_time        AS at_ms
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND NOT {_SAID_BY_HER}
   AND (cm.event_time, CAST(cm.common_message_id AS text))
       > (:after_ms, :after_id)
 ORDER BY cm.event_time DESC, cm.common_message_id DESC
 LIMIT :limit
"""

# "前面还有 N 条你没往回翻"那个 N。跟 :data:`_GLANCE_SQL` 必须是同一条未读判据 ——
# 两处口径一分家，她看到的数就跟她读到的东西对不上。
_UNREAD_COUNT_SQL = f"""
SELECT COUNT(*) FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND NOT {_SAID_BY_HER}
   AND (cm.event_time, CAST(cm.common_message_id AS text))
       > (:after_ms, :after_id)
"""


# 附件在她眼里叫什么。口径跟聊天那条路（``app.chat.content_parser`` 的
# ``ParsedContent.render``）一致，**但两边各写各的**：living 是独立一层，共用一份
# 代码换来的是改一处炸两处。
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
        # jsonb 里存的不保证是数组（同一条防线在 :data:`_UNREAD_SUMMARY_SQL` 的
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


def _one_message(row, *, now: datetime) -> str:
    """一条消息渲染成一行。"""
    return (
        f"{_clock(_instant(int(row['at_ms'])), now=now)} "
        f"{row['who']}：{_body_of(row)}"
    )


def _glance_text(*, title: str, rows: list, unread_before: int, now: datetime) -> str:
    """她这一眼看到的东西。行数有上限，跳过的那些**不会**在别处被补回来。"""
    lines = [_one_message(r, now=now) for r in reversed(rows)]
    head = f"「{title}」"
    skipped = unread_before - len(rows)
    if skipped > 0:
        head += f"（前面还有 {skipped} 条你没往回翻，就这么过去了）"
    return head + "\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# 按名字找回一条会话的地址
# ---------------------------------------------------------------------------

# 匹配两边：群按会话标题（群基本都有名），私聊按**在里面说过话的人**
# （``sender_display_name``，也正是信封上给她看的那个"谁"——她搜的名字和她见过的
# 名字是同一个）。私聊会话本身多半没有标题：prod 实测 205 条私聊里 158 条
# ``display_name`` 是空的，所以只查标题等于查不到人。
#
# 只在她手机上的会话里找（``common_bot_presence`` ＋ 她自己的 bot），跟
# :data:`_REACHABLE_SQL` 同一条口径 —— 查出来的地址必须是她真能用的，否则这只手
# 只是把 fail-loud 从"找不到"推迟到"发不出去"。
#
# **匹配的是 ``sender_display_name``，不是 ``common_user.display_name``。** 后者有索引、
# 表也小（prod 12973 行），但她从来没见过那个名字：信封和会话正文给她看的"谁"全都是
# ``sender_display_name``。两者在 prod 上 4652 组里有 2157 组不一致（46%），按 common_user
# 搜等于让她搜一个自己没见过的名字。
#
# 代价是没有索引可用，只能扫。**过滤必须下推进扫描**（``WHERE ... ILIKE`` 在 matched
# 里，不是先聚合再 FILTER）：prod 实测 akao 名下 323 条会话共 254 万条消息，下推之后
# 是一次并行 seq scan，EXPLAIN ANALYZE 386ms。这只手她一天调不了几次、不在每一缝的
# 路径上，386ms 换"她能主动找回一个人"是划算的——所以这里不加时间窗，加了她就再也
# 找不回久没联系的人。
_LOOK_UP_SQL = f"""
WITH mine AS (
  SELECT cc.common_conversation_id AS channel_id,
         cc.scope                  AS scope,
         COALESCE(cc.display_name, '') AS title
    FROM common_bot_presence bp
    JOIN bot_config bc
      ON bc.bot_name = bp.bot_name
     AND bc.persona_id = :persona_id
     AND bc.is_active = true
    JOIN common_conversation cc
      ON cc.common_conversation_id = bp.common_conversation_id
     AND cc.is_active = true
   WHERE bp.is_active = true
   GROUP BY cc.common_conversation_id, cc.scope, cc.display_name
),
matched AS (
  SELECT cm.common_conversation_id AS channel_id,
         COALESCE(cm.sender_display_name, '某人') AS who,
         MAX(cm.event_time) AS latest
    FROM common_message cm
    JOIN mine m ON m.channel_id = cm.common_conversation_id
   WHERE cm.sender_display_name ILIKE :like
     AND NOT {_SAID_BY_HER}
   GROUP BY 1, 2
)
SELECT m.channel_id AS channel_id,
       m.scope      AS scope,
       m.title      AS title,
       ARRAY_AGG(DISTINCT x.who) FILTER (WHERE x.who IS NOT NULL) AS matched,
       MAX(x.latest) AS latest
  FROM mine m
  LEFT JOIN matched x ON x.channel_id = m.channel_id
 GROUP BY m.channel_id, m.scope, m.title
HAVING m.title ILIKE :like OR COUNT(x.who) > 0
 ORDER BY m.channel_id
"""


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

    async with get_session() as s:
        rows = (
            await s.execute(
                text(_LOOK_UP_SQL),
                {
                    "persona_id": persona_id,
                    "like": f"%{wanted}%",
                    "own_bots": await _own_bot_names(persona_id=persona_id),
                },
            )
        ).mappings().all()

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
    """拿起手机看一条会话说了什么。

    信封只告诉你有动静、谁、多少条。**内容要调这个才有。**

    你看到的是最近的十来条。往前那些不会再回来——就像你真的划开一个未读很多的会话，
    扫一眼最后几条，前面的就那么过去了。

    你没调它的时候，消息照堆着、一条都不算你看过。

    Args:
        channel_id: 哪条会话（信封上那串）。

    Returns:
        这条会话最近的几条消息。
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
    window = {
        "channel_id": conv.channel_id,
        "after_ms": after_ms,
        "after_id": after_id,
        "own_bots": await _own_bot_names(persona_id=persona_id),
    }

    async with get_session() as s:
        rows = (
            await s.execute(
                text(_GLANCE_SQL), {**window, "limit": PHONE_GLANCE_LIMIT}
            )
        ).mappings().all()
        if not rows:
            return f"「{conv.title}」没有新消息。"

        unread_before = (
            await s.execute(text(_UNREAD_COUNT_SQL), window)
        ).scalar_one()

        seen = _glance_text(
            title=conv.title,
            rows=list(rows),
            unread_before=int(unread_before),
            now=now,
        )

    # 水位推到**未读里最新的那条**，不是"她读到的最后一条"：跳过的中间那些就此丢了。
    # 这正是这条设计要的行为，不是漏。
    #
    # **但不在这儿落库。** 工具返回不等于她看见了——结果还要进模型的上下文，这一缝才
    # 算真的把内容送到她眼前。当场提交的话，崩在中间就是"已读了但内容从没进过她的
    # 上下文"，那几条永久消失且一句报错都没有。所以攒进本缝状态，由 ``run_moment``
    # 的收尾跟 ``LifeMoment`` 一个事务落库（:func:`commit_glances`）。
    newest = rows[0]
    get_context().features.setdefault(FEATURE_GLANCES, []).append(
        {
            "lane": lane,
            "persona_id": persona_id,
            "channel_id": conv.channel_id,
            "read_through_message_id": str(newest["message_id"]),
            "moment_id": moment_id,
            "read_through_ms": int(newest["at_ms"]),
            "read_at": now,
        }
    )
    return seen


PHONE_TOOLS = [look_at_phone, look_up_contact]


# ---------------------------------------------------------------------------
# 她**已经知道**的那部分会话（嘴渲染措辞时能看的全部）
# ---------------------------------------------------------------------------

# 边界就是游标：她看过的（event_time <= 水位）+ **她自己**发出去的。**没看过的一个
# 字都不给**——嘴是用来把她的意思说成人话的，不是用来替她读消息的。给它未读内容，
# "内容要她去看"这条线当场就漏了：她会在措辞里回应一句自己根本没读过的话。
#
# 绕过游标那道门是给"她当然知道自己说过什么"留的，**只有她自己的话走得进来**。姐姐
# 的话从这道门溜进来会同时破两条线：白送未读内容，而且下面渲染时被署成"你"——她开口
# 前读到的上下文里，姐姐说的话写着是她自己说的。
_KNOWN_SQL = f"""
SELECT COALESCE(cm.sender_display_name, '某人') AS who,
       {_SAID_BY_HER}       AS said_by_you,
       cm.content           AS content,
       cm.content_text      AS content_text,
       cm.event_time        AS at_ms
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND (
        {_SAID_BY_HER}
        OR (cm.event_time, CAST(cm.common_message_id AS text))
           <= (:cursor_ms, :cursor_id)
   )
 ORDER BY cm.event_time DESC, cm.common_message_id DESC
 LIMIT :limit
"""

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

    没看过的消息不在里面。她从没看过、也没说过话的会话，返回一句如实的空。

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
    async with get_session() as s:
        rows = (
            await s.execute(
                text(_KNOWN_SQL),
                {
                    "channel_id": channel_id,
                    "cursor_ms": cursor_ms,
                    "cursor_id": cursor_id,
                    "own_bots": await _own_bot_names(persona_id=persona_id),
                    "limit": limit,
                },
            )
        ).mappings().all()
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
