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
过，这些 bot 都映射到 persona akao）。:func:`resolve_delivery` 先按**调用方 persona**
过滤出归属这个 persona 名下的 bot 私聊线（经 ``bot_config.persona_id``），只在这些线
内取最近一条投递——挑具体哪条线不是 life 的决策点（life 的决策是「发给谁」、由 uid
表达），是机制选归属自己名下最近活跃的一条；选不到任何归属该 persona 的 direct 会话
才是真正的不可投递。

**必须先按 persona 过滤、不能只取全局最近一条（曾是一个 bug）**：真人可能分别跟不
止一个姐妹的 bot 私聊过。如果不按 persona 过滤、直接取该 user 全部私聊线里全局最近
活跃的一条，会出现 A persona 想主动找这个人、但 B persona 的私聊线最近更活跃，导致
A 的话被用 B 的 bot 身份发出去——人设 / 视角串味。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text

from app.data.queries.persona import find_persona
from app.data.session import get_session
from app.infra.cst_time import to_cst
from app.life.feed_whitelist import (
    LIFE_FEED_CHAT_WHITELIST_KEY,
    parse_whitelist,
    should_feed_chat_to_life,
)

__all__ = [
    "PERSONA_UID_PREFIX",
    "USER_UID_PREFIX",
    "GROUP_UID_PREFIX",
    "RecipientCandidate",
    "MailboxTarget",
    "LarkP2PTarget",
    "GroupTarget",
    "UndeliverableRecipient",
    "persona_uid",
    "user_uid",
    "group_uid",
    "search_recipients",
    "resolve_delivery",
]

PERSONA_UID_PREFIX = "persona:"
USER_UID_PREFIX = "user:"
GROUP_UID_PREFIX = "group:"


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


@dataclass(frozen=True)
class GroupTarget:
    """``group:`` uid 的投递目标：往这个飞书群里发一条新消息（主动发，不 reply 某条）。

    ``common_conversation_id`` —— 群会话 id，worker 据它（is_proactive 路径）反查
        channel 私有映射拿飞书裸会话地址，不反查来源消息。
    ``bot_name`` —— **赤尾该 persona 在这个群里的 active 发送 bot 身份**。proactive
        出站不写 ``common_agent_response``，worker 没有别处可推断用哪个 bot——身份
        缺失会被 ack-drop 或用错 bot，所以解析阶段就把它钉死进 target（spec 决策 1）。
    ``display_name`` —— 群名（``common_conversation.display_name``）。proactive 渲染要把
        它当群场景的群名传进 inner_context（「在群聊『X』里说话」）；解析时顺手带出，
        免得 task 3 再查一次会话。群名可能为空（群没设名），渲染层自有缺席兜底。
    ``channel`` —— 渠道（当前只接飞书 = ``lark``）。

    task 3 拿它走 chat-response-worker 既有 is_proactive 出站路径（is_p2p=false、
    带 bot_name、chat_id=群 common_conversation_id），往群里发一条新消息。
    """

    common_conversation_id: str
    bot_name: str
    display_name: str = ""
    channel: str = "lark"


def persona_uid(persona_id: str) -> str:
    """三姐妹的稳定 uid：``persona:<persona_id>``。"""
    return f"{PERSONA_UID_PREFIX}{persona_id}"


def user_uid(common_user_id: object) -> str:
    """真人的稳定 uid：``user:<common_user_id>``。"""
    return f"{USER_UID_PREFIX}{common_user_id}"


def group_uid(common_conversation_id: object) -> str:
    """群的稳定 uid：``group:<common_conversation_id>``（对称 persona: / user:）。"""
    return f"{GROUP_UID_PREFIX}{common_conversation_id}"


def _persona_intro(display_name: str, persona_lite: str) -> str:
    """姐妹简介：显示名 + persona_lite 开头一句（帮 life 认人 / 区分重名）。

    persona_lite 是出厂身份速写，取它做简介让候选自带「这是谁」的辨识信息，不另
    建 contact 表（spec 决策 1：简介从现有 persona 资料取）。
    """
    head = persona_lite.strip().splitlines()[0] if persona_lite.strip() else ""
    return f"{display_name}（你的姐妹）：{head}" if head else f"{display_name}（你的姐妹）"


def _user_intro(display_name: str, *, last_dm_ms: int | None) -> str:
    """真人简介：显示名 + 与调用 persona 的私聊线事实（帮 life 区分重名、别编 uid）。

    common_user 只存 display_name —— 真人没有像 persona_lite 那样的身份正文，重名
    区分靠**私聊线事实**：``last_dm_ms`` 是「此刻调用 persona 名下 active bot 与 ta
    之间可投递的 direct + lark 私聊线」（存在判定同 :func:`_resolve_user` 的可投递
    口径）里**最近一条消息的毫秒时刻——双向、不限 role**（含 bot 主动出站行；只算
    ta 自己的发言会在"刚主动私聊过、ta 没回"时报旧日期，codex T3 必改 1），None =
    此刻没有这样的线。

    **措辞只陈述"此刻有没有能发过去的线"，不做"历史上从没私聊过"的断言**（spec
    决策 5）：线不存在可能是 bot 下线 / 那条线归属别的姐妹，不代表历史上没聊过——
    查询口径查的就是可投递现状，文案不能越过口径去说历史。日期按 CST 显示成自然
    中文（event_time 是 Unix 毫秒，经 :func:`app.infra.cst_time.to_cst` 归一）。
    """
    if last_dm_ms is None:
        return f"{display_name}（真人）——你这边没有能直接发过去的私聊线"
    dt = to_cst(str(last_dm_ms))
    if dt is None:
        # event_time 理论上总是合法毫秒；万一解析不出就退回不带日期的事实陈述
        # （不编日期、不静默吞掉"有线"这个事实）。
        return f"{display_name}（真人）——你这边有能直接发过去的私聊"
    return (
        f"{display_name}（真人）——你这边有能直接发过去的私聊，"
        f"最近一次是{dt.year}年{dt.month}月{dt.day}日"
    )


def _group_intro(display_name: str) -> str:
    """群简介：目前只有群名可用（display_name）。她靠群名认出要发哪个群。"""
    return f"{display_name}（飞书群）"


async def _load_chat_whitelist() -> frozenset[str]:
    """读 life 感知白名单集合（同 ``should_feed_chat_to_life`` 的 Dynamic Config 来源）。

    群的模糊查只在白名单内匹配（spec 决策 2：能听见才能说）。复用 feed_whitelist 的
    key + ``parse_whitelist``（单一来源，不另起一套配置），同样 ``asyncio.to_thread``
    避免缓存刷新阻塞事件循环。空 / 缺失 → 空集合（fail-closed：没有任何群可查）。
    """
    import asyncio

    from inner_shared.dynamic_config import dynamic_config

    raw = await asyncio.to_thread(
        dynamic_config.get, LIFE_FEED_CHAT_WHITELIST_KEY, default="",
    )
    return parse_whitelist(raw)


async def search_recipients(
    query: str, *, persona_id: str
) -> list[RecipientCandidate]:
    """按名字模糊查可发送对象，返回候选列表（typed uid + 简介）。

    匹配口径：对显示名做大小写无关的子串匹配（``ILIKE %query%``），姐妹查
    ``bot_persona.display_name``、真人查 ``common_user.display_name``。空 / 全空白
    query 直接返回空（没给名字就没有候选）。

    ``persona_id`` 是**调用方 persona**（谁在查）：真人候选的简介按它附「此刻你名下
    有没有能直接发过去的私聊线 + 最近一次时间」的事实（帮 life 区分重名、别盲选），
    口径复用 :func:`_resolve_user` 的可投递判定（direct + lark + bot_config 归属该
    persona 且 active）。姐妹 / 群候选不消费它。

    **只返回候选，不做任何替 life 决策的事**（赤尾设计宪法）：不按亲密度 / 活跃度 /
    兴趣排序、不筛、不自动取第一个。返回顺序是稳定的纯机制序（姐妹在前按
    persona_id 升序，真人在后按 common_user_id 升序），只为输出确定性，不含「谁更
    该被选」的语义——重名 / 多候选时全列出来交给 life 自己挑。私聊线事实只进 intro
    文案，**绝不影响候选顺序**。
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

        # 真人候选的私聊线事实（persona 感知）：对整批命中真人**一次批量查**「该真人 ×
        # 调用 persona 名下 active bot 之间有没有 direct + lark 私聊线 + 最近一次消息时间」，
        # 不逐候选 N+1。两层结构：
        #
        #   1. 内层 pair：线的**存在判定**逐字对齐 _resolve_user 的可投递口径
        #      （scope='direct' + channel='lark' + bot_config(persona_id, is_active)），
        #      定位 (真人, 可投递会话) 对——查的是「此刻能不能发过去」，不是「历史上
        #      聊没聊过」，intro 措辞按前者陈述（spec 决策 5）。
        #   2. 外层对会话**全消息**聚合 MAX(event_time)——「最近一次」必须**双向、不限
        #      role**（codex T3 必改 1）：proactive 出站 assistant 行的 common_user_id
        #      是 bot 自己的，只按真人自己的发言行聚合会漏掉它，「赤尾昨天刚主动私聊
        #      过、ta 没回」时日期停在 ta 上次发言，恰好在主动私聊场景失真。
        #
        # 参数形态（codex T3 必改 2）：common_message.common_user_id 是带索引的 uuid
        # 列，**列侧裸用、参数侧给 uuid 列表**（PG 从 `= ANY($n)` 推出 uuid[]，asyncpg
        # 直接编码 UUID 对象）——列侧 CAST 成 text 会绕开索引走全表扫。上面群白名单的
        # `CAST(... AS text) = ANY(:wl)` 是小表（common_conversation）先例，别学到大表上。
        last_dm_by_user: dict[str, int] = {}
        if user_rows:
            dm_rows = (
                await s.execute(
                    text(
                        "SELECT pair.common_user_id AS uid, "
                        "       MAX(m.event_time) AS last_event "
                        "FROM ("
                        "  SELECT DISTINCT cm.common_user_id, "
                        "         cm.common_conversation_id "
                        "  FROM common_message cm "
                        "  JOIN common_conversation cc "
                        "    ON cc.common_conversation_id = cm.common_conversation_id "
                        "   AND cc.scope = 'direct' "
                        "   AND cc.channel = 'lark' "
                        "  JOIN bot_config bc "
                        "    ON bc.bot_name = cm.bot_name "
                        "   AND bc.persona_id = :pid "
                        "   AND bc.is_active = true "
                        "  WHERE cm.common_user_id = ANY(:uids) "
                        ") pair "
                        "JOIN common_message m "
                        "  ON m.common_conversation_id = pair.common_conversation_id "
                        "GROUP BY pair.common_user_id"
                    ),
                    {
                        "pid": persona_id,
                        "uids": [r["common_user_id"] for r in user_rows],
                    },
                )
            ).mappings().all()
            last_dm_by_user = {str(r["uid"]): r["last_event"] for r in dm_rows}

        for row in user_rows:
            name = row["display_name"] or ""
            candidates.append(
                RecipientCandidate(
                    uid=user_uid(row["common_user_id"]),
                    display_name=name,
                    intro=_user_intro(
                        name,
                        last_dm_ms=last_dm_by_user.get(str(row["common_user_id"])),
                    ),
                )
            )

        # 群：只匹配**白名单内**的 scope='group' + channel='lark' 群（spec 决策 2：
        # 她能查到的群严格等于她能听见的群）。白名单空 → 跳过群匹配（不查 DB）。群名
        # 子串匹配，common_conversation_id 升序（稳定机制序，同人候选不排序不取第一）。
        whitelist = await _load_chat_whitelist()
        if whitelist:
            group_rows = (
                await s.execute(
                    text(
                        "SELECT common_conversation_id, display_name "
                        "FROM common_conversation "
                        "WHERE scope = 'group' "
                        "  AND channel = 'lark' "
                        "  AND is_active = true "
                        "  AND display_name ILIKE :like "
                        "  AND CAST(common_conversation_id AS text) "
                        "      = ANY(:wl) "
                        "ORDER BY common_conversation_id ASC"
                    ),
                    {"like": like, "wl": list(whitelist)},
                )
            ).mappings().all()
            for row in group_rows:
                name = row["display_name"] or ""
                candidates.append(
                    RecipientCandidate(
                        uid=group_uid(row["common_conversation_id"]),
                        display_name=name,
                        intro=_group_intro(name),
                    )
                )

    return candidates


async def resolve_delivery(
    uid: str, *, persona_id: str | None = None
) -> MailboxTarget | LarkP2PTarget | GroupTarget:
    """把一个 typed uid 解析成此刻的投递目标，查不到 / 不可投递则 fail-loud。

    ``persona:<id>`` → :class:`MailboxTarget`（信箱，不需要 ``persona_id``）；
    ``user:<common_user_id>`` → :class:`LarkP2PTarget`（飞书私聊，需已有归属**调用方
        persona** 自己 bot 的 p2p 会话——所以 user 分支也必须给 ``persona_id``，缺失
        fail-loud）；
    ``group:<common_conversation_id>`` → :class:`GroupTarget`（飞书群，需白名单 +
        scope=group + channel=lark + active，且解析出**调用方 persona** 在该群的 active
        bot——所以群分支同样必须给 ``persona_id``）。

    任何「发不了」都抛 :class:`UndeliverableRecipient`，``str(exc)`` 是给 life 看的
    原因——绝不返回伪地址、不静默降级（spec 决策 2 / 6）。
    """
    if uid.startswith(PERSONA_UID_PREFIX):
        target_persona = uid[len(PERSONA_UID_PREFIX):]
        return await _resolve_persona(target_persona, uid)
    if uid.startswith(USER_UID_PREFIX):
        user_id = uid[len(USER_UID_PREFIX):]
        return await _resolve_user(user_id, uid, persona_id)
    if uid.startswith(GROUP_UID_PREFIX):
        conv_id = uid[len(GROUP_UID_PREFIX):]
        return await _resolve_group(conv_id, uid, persona_id)
    raise UndeliverableRecipient(
        f"uid={uid!r} 不是合法身份句柄（persona:<id> / user:<id> / group:<id>），发不了。"
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


async def _resolve_user(
    user_id: str, uid: str, persona_id: str | None
) -> LarkP2PTarget:
    """user uid → 飞书私聊目标。

    解析步骤：先确认这个 common_user 存在；再找 ta 跟**当前发送 persona 自己的
    bot**之间最近一条 ``scope='direct'`` 的私聊会话。没有任何这样的 direct 会话 =
    不可投递（这边没 open_id 起不了新私聊，见模块 docstring 的 gap），fail-loud。

    **必须按 persona 过滤 bot，不能只取全局最近一条（曾经的 bug）**：一个真人可能
    分别跟不止一个姐妹的 bot 私聊过（这些 bot 各自映射到不同 persona）。之前的实现
    取该 user 所有私聊线里全局最近活跃的一条、不管是谁的线——结果 A persona 想主动
    找这个人时，如果 B persona 的私聊线最近更活跃，会把 A 的话用 B 的 bot 身份发出去
    （人设 / 视角串味）。现在跟群分支（``_resolve_group``）对齐：出站身份必须钉死在
    调用方 persona 自己名下的 bot，选最近只在这些线内部选，选不到才是真不可投递。
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

    if not persona_id:
        raise UndeliverableRecipient(
            f"uid={uid!r} 解析私聊投递缺 persona_id（解析不出该用哪个 bot 发），发不了。"
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

        # 2) 归属调用方 persona 的私聊会话里最近一条 + 发送 bot：按这个 user 在 direct
        #    会话里出现过的消息找，**JOIN bot_config 限定 bot_name 归属当前 persona_id**
        #    （bot 修复根因），再在这些线内取最近活跃（max event_time）那条的 bot_name。
        #    bot_name 取该会话里非空的一个（一条 p2p 会话只有一个 bot，见数据查实）。
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
                    "JOIN bot_config bc "
                    "  ON bc.bot_name = cm.bot_name "
                    " AND bc.persona_id = :pid "
                    " AND bc.is_active = true "
                    "WHERE cm.common_user_id = CAST(:uid AS uuid) "
                    "GROUP BY cm.common_conversation_id "
                    "HAVING MAX(cm.bot_name) IS NOT NULL "
                    "ORDER BY last_event DESC "
                    "LIMIT 1"
                ),
                {"uid": user_id, "pid": persona_id},
            )
        ).mappings().first()

    if row is None:
        raise UndeliverableRecipient(
            f"{uid!r} 没有跟 persona={persona_id!r} 自己的 bot 之间可投递的飞书私聊会话"
            "（从没私聊过、那条私聊查不到发送 bot、或私聊过的是别的姐妹的 bot），"
            "发不了——这边只能发已经私聊过的人，没法主动开一个新私聊。"
        )

    return LarkP2PTarget(
        common_conversation_id=str(row["conv_id"]),
        bot_name=row["bot_name"],
        user_id=user_id,
        channel=row["channel"] or "lark",
    )


async def _resolve_group(
    conv_id: str, uid: str, persona_id: str | None
) -> GroupTarget:
    """group uid → 飞书群投递目标，安全闸 + 出站身份解析都在这一关（spec 决策 1 / 2）。

    这是**真正的安全闸**（codex T1 必改）：``send_message(group:<id>)`` 是模型直接调的，
    她可能从别处拿到、甚至编出一个非白名单群 id 绕过 look_up，所以投递最后一关硬校验：

      1. uid 形完整（conv_id 非空、是 uuid 形）——脏串不往 SQL CAST 送；
      2. **在当前 life 感知白名单**（同 ``should_feed_chat_to_life`` 来源，能听见才能说）；
      3. 该会话 **scope='group' + channel='lark' + is_active**；
      4. 解析出**调用方 persona 当前还在这个群且 active 的 bot_name**（出站身份必须确定：
         proactive 不写 common_agent_response、worker 没别处推断 bot，缺失会被 ack-drop /
         用错 bot）。bot 是否还在群对齐 ``common_bot_presence`` 口径（同 persona.py 的
         ``resolve_bot_name_for_persona``）—— 只看历史回复不够，bot 被移出群后历史还在但
         发不出去，必须投递前 fail-loud 而非让 worker 异步发失败（codex 必改 2）。

    任一不满足 → fail-loud（抛 :class:`UndeliverableRecipient`），绝不返回伪地址。
    """
    if not conv_id:
        raise UndeliverableRecipient(f"uid={uid!r} 缺群 id，发不了。")

    # 群分支必须知道是哪个 persona 在发（解析 ta 在这个群的 active bot）。
    if not persona_id:
        raise UndeliverableRecipient(
            f"uid={uid!r} 解析群投递缺 persona_id（解析不出该用哪个 bot 发），发不了。"
        )

    # 坏 uid 在解析阶段就识别（同 _resolve_user）：脏串往 SQL CAST 会冒底层 DB 错。
    try:
        UUID(conv_id)
    except (ValueError, AttributeError, TypeError):
        raise UndeliverableRecipient(
            f"uid={uid!r} 里的群 id 不是合法 id（不是 uuid 形），发不了。"
        ) from None

    # 安全闸第一关：白名单（同 should_feed_chat_to_life 的 fail-closed 来源）。白名单
    # 外 / 配置空都在这里挡死（绝不投递到非白名单群）。
    if not await should_feed_chat_to_life(chat_id=conv_id, is_p2p=False):
        raise UndeliverableRecipient(
            f"群 {uid!r} 不在你能听见 / 能说话的范围里（不在白名单内，或白名单未配置），"
            f"发不了——你能发的群严格等于你能听见的群（配置 key={LIFE_FEED_CHAT_WHITELIST_KEY}）。"
        )

    async with get_session() as s:
        # 安全闸第二关：scope='group' + channel='lark' + active（白名单里混进 direct /
        # 非 lark / 已解散都在这里挡）。顺手取群名（display_name）带进 target，免得 task 3
        # 再查一次会话。
        conv = (
            await s.execute(
                text(
                    "SELECT display_name FROM common_conversation "
                    "WHERE common_conversation_id = CAST(:cid AS uuid) "
                    "  AND scope = 'group' "
                    "  AND channel = 'lark' "
                    "  AND is_active = true "
                    "LIMIT 1"
                ),
                {"cid": conv_id},
            )
        ).mappings().first()
        if conv is None:
            raise UndeliverableRecipient(
                f"群 {uid!r} 不是一个可投递的飞书群（不存在 / 不是群聊 / 非飞书 / 已解散），"
                "发不了。"
            )

        # 出站身份解析：该 persona 在这个群里**当前还在群且 active** 的发送 bot。两道并存：
        #
        #   1. **persona 归属**：发言 persona 经 COALESCE(common_agent_response.persona_id,
        #      bot_config.persona_id) 取（与 find_recent_chat_messages 同口径）——proactive
        #      出站行 response_id=NULL 时靠 bot_config 兜底。取该 persona 最近一条 assistant
        #      回复的 bot_name。
        #   2. **当前在群闸（codex 必改 2）**：光看历史回复 + bot_config.is_active 不够 —— bot
        #      被移出群后历史回复还在、bot_config 也可能仍 active，但它已不在这个群、发不出去。
        #      JOIN common_bot_presence 限定 bp.common_conversation_id = 本群 + bp.is_active
        #      = true（口径对齐 app/data/queries/persona.py 的 resolve_bot_name_for_persona），
        #      让解析出的 bot 必须当前在群且 active；不在 / 已退群 → 这个 candidate 被滤掉，
        #      没有任何候选时 fail-loud（投递前挡，不让 worker 异步发失败）。
        bot_row = (
            await s.execute(
                text(
                    "SELECT cm.bot_name AS bot_name "
                    "FROM common_message cm "
                    "JOIN common_bot_presence bp "
                    "  ON bp.bot_name = cm.bot_name "
                    " AND bp.common_conversation_id = CAST(:cid AS uuid) "
                    " AND bp.is_active = true "
                    "LEFT JOIN common_agent_response car "
                    "  ON cm.response_id = car.session_id "
                    "WHERE cm.common_conversation_id = CAST(:cid AS uuid) "
                    "  AND cm.role = 'assistant' "
                    "  AND cm.bot_name IS NOT NULL "
                    "  AND COALESCE("
                    "        car.persona_id, "
                    "        (SELECT bc.persona_id FROM bot_config bc "
                    "         WHERE bc.bot_name = cm.bot_name AND bc.is_active = true "
                    "         LIMIT 1)"
                    "      ) = :pid "
                    "ORDER BY cm.event_time DESC "
                    "LIMIT 1"
                ),
                {"cid": conv_id, "pid": persona_id},
            )
        ).mappings().first()

    if bot_row is None or not bot_row["bot_name"]:
        raise UndeliverableRecipient(
            f"在群 {uid!r} 里找不到你（persona={persona_id!r}）当前可用的发送身份（你没在"
            "这个群发过言，或那个 bot 已经不在这个群了），发不了。"
        )

    return GroupTarget(
        common_conversation_id=conv_id,
        bot_name=bot_row["bot_name"],
        display_name=conv["display_name"] or "",
        channel="lark",
    )
