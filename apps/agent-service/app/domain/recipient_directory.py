"""可发送对象目录 —— 把「名字 / 身份」解析成「该怎么把消息送到 ta」(life 主动发消息 task 2).

life 想给谁说话时报一个名字，这一层负责两件事，**只解析、不发送**（发送是 task 3）：

  1. :func:`search_recipients` —— 按名字模糊查到候选（typed uid + 一段简介帮 life
     认人 / 区分重名）。**只返回候选**，绝不排序、不按亲密度 / 活跃度 / 兴趣筛、不
     自动取第一个——选谁是 life 自己的决定（赤尾设计宪法：代码不替 agent 做决定）。
  2. :func:`resolve_delivery` —— 把一个 typed uid 解析成此刻的投递目标。查不到 /
     不可投递时 **fail-loud**（抛 :class:`UndeliverableRecipient`，明确报「发不了 +
     原因」），绝不返回伪地址、不静默降级。

typed uid 是稳定的、渠道无关的身份句柄：

  * ``persona:<persona_id>`` —— 三姐妹（akao / chinagi / ayana），身份与简介取自
    ``bot_persona``。解析成 :class:`MailboxTarget`（对接 life 信箱
    :func:`app.data.queries.mailbox.deliver_event`）。
  * ``user:<common_user_id>`` —— 真人（含 bezhai，他在这里就是一个普通 user、不
    特殊）。解析成 :class:`LarkP2PTarget`（飞书私聊投递目标）。

真人飞书私聊地址来源（**查实结论 + gap**）
------------------------------------------------------------------------------
agent-service 只持有 common_* 表。``common_user`` 的列只有 ``common_user_id /
channel / display_name / avatar_url``——**没有 open_id**。飞书 open_id 与会话裸
地址活在 channel-server 的私有映射表里，agent-service 这边查不到。

由此真人投递的能力边界（**这是真实约束，不是偷懒**）：

  * **只能发「已经有过 p2p 私聊会话」的真人**。判据：该 user 在某条
    ``common_message`` 里出现过、且那条消息所在的 ``common_conversation`` 是
    ``scope='direct'``（私聊）。这条 direct 会话的 ``common_conversation_id`` +
    会话里 bot 的 ``bot_name`` 就是可投递目标——chat-response-worker 的
    ``resolveOutboundTarget`` 会拿 common_conversation_id 反查 channel 私有映射、
    得到飞书裸会话地址，再用 bot_name 选发送身份（见
    ``apps/channel-server/src/workers/chat-response-worker.ts`` 的 is_proactive 出站）。
  * **不能用 open_id 给「没私聊过的真人」主动发起新私聊**——这边根本拿不到
    open_id。群里见过但从没私聊过的真人 → 不可投递（fail-loud）。

> **gap（影响 task 4 真人出站）**：要支持「对从没私聊过的真人发起新私聊」，得让
> agent-service 能拿到 open_id（要么 common_user 补 open_id 列由 channel-server
> 回填，要么 agent-service 调 channel-server 的某个接口按 user 拿 open_id +
> 起会话）。**本 task 不补这个存储**（spec：先不建表）；当前只能复投已有 p2p 会话。

发送 bot 的选取
------------------------------------------------------------------------------
一个真人可能与多个 bot 各有一条 p2p 会话（如 bezhai 同时跟 chiwei / dev / fly 私聊
过，这些 bot 都映射到 persona akao）。:func:`resolve_delivery` 取该 user **最近**一条
direct 会话的 bot 投递——「她主动找这个真人」用对方最近在用的那条私聊线最自然。挑
具体哪条线不是 life 的决策点（life 的决策是「发给谁」、由 uid 表达），是机制选最近
活跃的一条；选不到任何 direct 会话才是真正的不可投递。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.data.queries.persona import find_persona
from app.data.session import get_session

__all__ = [
    "PERSONA_UID_PREFIX",
    "USER_UID_PREFIX",
    "RecipientCandidate",
    "MailboxTarget",
    "LarkP2PTarget",
    "UndeliverableRecipient",
    "persona_uid",
    "user_uid",
    "search_recipients",
    "resolve_delivery",
]

PERSONA_UID_PREFIX = "persona:"
USER_UID_PREFIX = "user:"


class UndeliverableRecipient(Exception):
    """一个 uid 此刻发不了——fail-loud 信号（明确报「发不了 + 原因」）。

    task 3 把它当工具错误喂回 life，让她自己处置（换个人 / 重试 / 算了），绝不
    静默降级、不替她另找目标（spec 决策 6）。``str(exc)`` 就是给 life 看的原因。
    """


@dataclass(frozen=True)
class RecipientCandidate:
    """一个可发送对象的候选：稳定 uid + 一段帮 life 认人 / 区分重名的简介。

    candidate 只描述「这是谁」，**不含投递地址**（地址要到真发的时刻由
    :func:`resolve_delivery` 解析，且可能不可投递）。``intro`` 是自然语言一段话，
    给 life 读懂「这是哪个人」用——重名时两个候选靠各自 intro 区分。
    """

    uid: str
    display_name: str
    intro: str


@dataclass(frozen=True)
class MailboxTarget:
    """``persona:`` uid 的投递目标：往这个 persona 的 life 信箱投。

    task 3 拿它对接 :func:`app.data.queries.mailbox.deliver_event`
    （``persona_id`` 即收件人信箱）。
    """

    persona_id: str


@dataclass(frozen=True)
class LarkP2PTarget:
    """``user:`` uid 的投递目标：发到这个真人的飞书私聊。

    ``common_conversation_id`` —— 已有的 direct（p2p）会话 id，worker 反查 channel
        私有映射拿飞书裸会话地址。
    ``bot_name`` —— 这条私聊里的发送 bot 身份。
    ``channel`` —— 渠道（当前只接飞书 = ``lark``）。
    ``user_id`` —— 收件真人的 ``common_user_id``（透传，便于 task 3 拼出站 payload）。

    task 3 拿它走 chat-response-worker 既有 is_proactive 出站路径（is_p2p=true、
    带 bot_name、chat_id=common_conversation_id），不靠伪 id。
    """

    common_conversation_id: str
    bot_name: str
    user_id: str
    channel: str = "lark"


def persona_uid(persona_id: str) -> str:
    """三姐妹的稳定 uid：``persona:<persona_id>``。"""
    return f"{PERSONA_UID_PREFIX}{persona_id}"


def user_uid(common_user_id: object) -> str:
    """真人的稳定 uid：``user:<common_user_id>``。"""
    return f"{USER_UID_PREFIX}{common_user_id}"


def _persona_intro(display_name: str, persona_lite: str) -> str:
    """姐妹简介：显示名 + persona_lite 开头一句（帮 life 认人 / 区分重名）。

    persona_lite 是出厂身份速写，取它做简介让候选自带「这是谁」的辨识信息，不另
    建 contact 表（spec 决策 1：简介从现有 persona 资料取）。
    """
    head = persona_lite.strip().splitlines()[0] if persona_lite.strip() else ""
    return f"{display_name}（你的姐妹）：{head}" if head else f"{display_name}（你的姐妹）"


def _user_intro(display_name: str) -> str:
    """真人简介：目前只有显示名可用（common_user 没有更多资料）。

    common_user 只存 display_name —— 真人没有像 persona_lite 那样的身份正文。重名
    真人当前只能靠显示名区分；要更强的区分信息（最近聊天上下文等）是后续增量，本
    task 不补存储（spec：先不建表）。
    """
    return f"{display_name}（真人）"


async def search_recipients(query: str) -> list[RecipientCandidate]:
    """按名字模糊查可发送对象，返回候选列表（typed uid + 简介）。

    匹配口径：对显示名做大小写无关的子串匹配（``ILIKE %query%``），姐妹查
    ``bot_persona.display_name``、真人查 ``common_user.display_name``。空 / 全空白
    query 直接返回空（没给名字就没有候选）。

    **只返回候选，不做任何替 life 决策的事**（赤尾设计宪法）：不按亲密度 / 活跃度 /
    兴趣排序、不筛、不自动取第一个。返回顺序是稳定的纯机制序（姐妹在前按
    persona_id 升序，真人在后按 common_user_id 升序），只为输出确定性，不含「谁更
    该被选」的语义——重名 / 多候选时全列出来交给 life 自己挑。
    """
    q = (query or "").strip()
    if not q:
        return []

    like = f"%{q}%"
    candidates: list[RecipientCandidate] = []

    async with get_session() as s:
        # 姐妹：bot_persona 按 display_name 子串匹配，persona_id 升序（稳定机制序）。
        persona_rows = (
            await s.execute(
                text(
                    "SELECT persona_id, display_name, persona_lite "
                    "FROM bot_persona "
                    "WHERE display_name ILIKE :like "
                    "ORDER BY persona_id ASC"
                ),
                {"like": like},
            )
        ).mappings().all()
        for row in persona_rows:
            candidates.append(
                RecipientCandidate(
                    uid=persona_uid(row["persona_id"]),
                    display_name=row["display_name"],
                    intro=_persona_intro(row["display_name"], row["persona_lite"] or ""),
                )
            )

        # 真人：common_user 按 display_name 子串匹配，common_user_id 升序。
        user_rows = (
            await s.execute(
                text(
                    "SELECT common_user_id, display_name "
                    "FROM common_user "
                    "WHERE display_name ILIKE :like "
                    "ORDER BY common_user_id ASC"
                ),
                {"like": like},
            )
        ).mappings().all()
        for row in user_rows:
            name = row["display_name"] or ""
            candidates.append(
                RecipientCandidate(
                    uid=user_uid(row["common_user_id"]),
                    display_name=name,
                    intro=_user_intro(name),
                )
            )

    return candidates


async def resolve_delivery(uid: str) -> MailboxTarget | LarkP2PTarget:
    """把一个 typed uid 解析成此刻的投递目标，查不到 / 不可投递则 fail-loud。

    ``persona:<id>`` → :class:`MailboxTarget`（信箱）；
    ``user:<common_user_id>`` → :class:`LarkP2PTarget`（飞书私聊，需已有 p2p 会话）。

    任何「发不了」都抛 :class:`UndeliverableRecipient`，``str(exc)`` 是给 life 看的
    原因——绝不返回伪地址、不静默降级（spec 决策 2 / 6）。
    """
    if uid.startswith(PERSONA_UID_PREFIX):
        persona_id = uid[len(PERSONA_UID_PREFIX):]
        return await _resolve_persona(persona_id, uid)
    if uid.startswith(USER_UID_PREFIX):
        user_id = uid[len(USER_UID_PREFIX):]
        return await _resolve_user(user_id, uid)
    raise UndeliverableRecipient(
        f"uid={uid!r} 不是合法身份句柄（要么 persona:<id>、要么 user:<id>），发不了。"
    )


async def _resolve_persona(persona_id: str, uid: str) -> MailboxTarget:
    """persona uid → 信箱目标；persona_id 不存在（含空）即不可投递。"""
    if not persona_id:
        raise UndeliverableRecipient(f"uid={uid!r} 缺 persona_id，发不了。")
    persona = await find_persona(persona_id)
    if persona is None:
        raise UndeliverableRecipient(
            f"没有叫 {persona_id!r} 的姐妹（uid={uid!r}），发不了——换一个再试。"
        )
    return MailboxTarget(persona_id=persona_id)


async def _resolve_user(user_id: str, uid: str) -> LarkP2PTarget:
    """user uid → 飞书私聊目标。

    解析步骤：先确认这个 common_user 存在；再找 ta 最近一条 ``scope='direct'``
    的私聊会话 + 会话里的发送 bot。没有任何 direct 会话 = 不可投递（这边没 open_id
    起不了新私聊，见模块 docstring 的 gap），fail-loud。
    """
    if not user_id:
        raise UndeliverableRecipient(f"uid={uid!r} 缺 common_user_id，发不了。")

    # 坏 uid 在解析阶段就识别（codex 建议 2）：common_user_id 是 uuid，非 uuid 串往下
    # 会进 SQL ``CAST(:uid AS uuid)`` 冒一个底层 DB 错（穿出 @tool_error 兜底之外、
    # 给 life 看的是难懂的数据库报错）。这里先验形 —— 解析不出 uuid 就 fail-loud，把
    # 「这不是一个合法的人 id、发不了」作为清晰原因喂回 life。
    try:
        UUID(user_id)
    except (ValueError, AttributeError, TypeError):
        raise UndeliverableRecipient(
            f"uid={uid!r} 里的 common_user_id 不是合法 id（不是 uuid 形），发不了——"
            "换一个再试。"
        )

    async with get_session() as s:
        # 1) user 存在性：不存在直接 fail-loud（别拿一个不存在的 user 去找会话）。
        exists = (
            await s.execute(
                text(
                    "SELECT 1 FROM common_user "
                    "WHERE common_user_id = CAST(:uid AS uuid) LIMIT 1"
                ),
                {"uid": user_id},
            )
        ).scalar_one_or_none()
        if exists is None:
            raise UndeliverableRecipient(
                f"没有 common_user={user_id!r}（uid={uid!r}），发不了。"
            )

        # 2) 最近一条 direct 私聊会话 + 发送 bot：按这个 user 在 direct 会话里出现过
        #    的消息找，取最近活跃（max event_time）那条会话的 bot_name。bot_name 取
        #    该会话里非空的一个（一条 p2p 会话只有一个 bot，见数据查实）。
        #    **渠道限定 lark（codex 必改 1）**：可投递判定不能只看「有没有 direct 会话」，
        #    还得限定 ``channel='lark'`` —— 主动发的出站路径（chat-response-worker 的
        #    is_proactive 分支）只走飞书；非 lark 渠道的 direct 会话这边没有送达通路，
        #    当它可投递会 emit 一条永远送不出去的出站段。这一刀只接飞书，非 lark 的
        #    direct 会话不算可投递（下面 row is None → fail-loud）。
        row = (
            await s.execute(
                text(
                    "SELECT cm.common_conversation_id AS conv_id, "
                    "       MAX(cm.event_time) AS last_event, "
                    "       MAX(cm.bot_name) AS bot_name, "
                    "       MAX(cc.channel) AS channel "
                    "FROM common_message cm "
                    "JOIN common_conversation cc "
                    "  ON cc.common_conversation_id = cm.common_conversation_id "
                    " AND cc.scope = 'direct' "
                    " AND cc.channel = 'lark' "
                    "WHERE cm.common_user_id = CAST(:uid AS uuid) "
                    "GROUP BY cm.common_conversation_id "
                    "HAVING MAX(cm.bot_name) IS NOT NULL "
                    "ORDER BY last_event DESC "
                    "LIMIT 1"
                ),
                {"uid": user_id},
            )
        ).mappings().first()

    if row is None:
        raise UndeliverableRecipient(
            f"{uid!r} 没有可投递的飞书私聊会话（从没私聊过、或那条私聊查不到发送 bot），"
            "发不了——这边只能发已经私聊过的人，没法主动开一个新私聊。"
        )

    return LarkP2PTarget(
        common_conversation_id=str(row["conv_id"]),
        bot_name=row["bot_name"],
        user_id=user_id,
        channel=row["channel"] or "lark",
    )
