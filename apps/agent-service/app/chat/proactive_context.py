"""Proactive (life → 真人) context builder — the life-intent half of an outbound turn.

This is the **what** half of a *proactive* chat turn (decision 2 in the
proactive-chat spec). It is a **separate** context builder, parallel to
``app.chat.context.build_human_chat_context`` — it does **not** reuse that one and
does **not** touch a source message. Where the human-chat builder is keyed on a
source ``message_id`` (a real person sent something, 赤尾 replies), this one is
keyed on a life **intent** (赤尾 decides on her own to reach out) plus the chat's
history fetched by ``chat_id`` alone.

It owns, and only owns:
  - fetch recent history by ``chat_id`` (``find_recent_chat_messages``, no source
    message)
  - build the LLM message list, attributing **赤尾's own past messages (including
    a previous proactive) as ASSISTANT** and the real person as USER — so the
    render model never mistakes her own words for the person's input
  - append the life **intent** as the final USER-role framing message: that is the
    trigger that drives the render layer to produce her outbound, in-persona text
  - resolve the persona bundle (identity / appearance / persona object)
  - assemble ``inner_context`` (scene + life state + pages) for this turn

The result is the same :class:`~app.chat.render.ChatTurnContext` shape the human
path produces, consumed uniformly by ``app.chat.render.render_chat_turn``. Life
gives the *what*; render gives the *how*.
"""

from __future__ import annotations

import logging

from app.agent.neutral import ContentBlock, Message, Role
from app.chat._context_messages import format_message_tag
from app.chat.content_parser import parse_content
from app.chat.render import ChatTurnContext
from app.data.queries import find_recent_chat_messages, find_username
from app.memory._persona import load_persona
from app.memory.context import build_inner_context
from app.memory.identity_registry import get_relation

logger = logging.getLogger(__name__)

# 意图框架消息的标头：明确这不是真人发来的话，而是赤尾此刻自己想主动开口的要点 ——
# 让渲染模型据此**产出她对真人说出口的话**（第一人称、人设口径），而不是去回复一条
# 「别人的消息」。机制层框架文案、零剧情事实（同 inner_context 各段标头的宪法）。
_INTENT_HEADER = (
    "【你现在想主动开口】没人在等你回话——是你自己这会儿想主动找对方说点什么。"
    "你想说的要点是：\n"
)
_INTENT_FOOTER = (
    "\n\n按这个要点，用你自己的话、你平时的口吻，自然地主动开口说出来。"
)


async def _history_messages(
    history: list[tuple[object, str | None]],
    *,
    persona_id: str,
) -> list[Message]:
    """把 chat_id 历史渲染成 LLM 消息：赤尾自己说的 → ASSISTANT，真人 → USER。

    承重红线（spec Task 2）：history 要把赤尾自己发过的（含上一条 proactive）认作她
    自己说的、**不要当成真人输入**。判据同 ``build_p2p_messages``：``role ==
    "assistant"`` 且发言 persona == 当前 persona_id → ASSISTANT；其余 → USER。每条
    取纯文本（proactive 历史不带图——主动发是文字消息，省掉收图链路）。空文本的条目
    （纯图 / 表情渲染为空）跳过。

    Task 3：真人发言（USER 行）渲染成结构化标签，rel 只来自 ``get_relation``
    （按 ``record.user_id`` = common_user_id，命中主人 → owner），用户字串全转义；
    赤尾自己的话（ASSISTANT）认作她自己说的、不盖 rel、文本原样。fail-closed：拿不到
    common_user_id / 非主人 → rel 空，绝不回退显示名当身份。``persona_id`` 仅用于判定
    哪条历史是赤尾自己说的（``is_self``），与身份 rel 无关。
    """
    result: list[Message] = []
    for record, msg_persona in history:
        text = parse_content(record.content).render()
        if not text:
            continue
        is_self = (
            record.role == "assistant"
            and bool(msg_persona)
            and msg_persona == persona_id
        )
        if is_self:
            # 赤尾自己的话：ASSISTANT 角色已表明是她说的，文本原样、不套对方署名标签。
            result.append(
                Message(role=Role.ASSISTANT, content=[ContentBlock.from_text(text)])
            )
            continue
        # 真人发言：结构化署名。rel 按 common_user_id 盖章，拿不到 id → fail-closed None。
        common_user_id = getattr(record, "user_id", None)
        rel = await get_relation(common_user_id) if common_user_id else None
        speaker = getattr(record, "username", None) or "未知用户"
        tag = format_message_tag(speaker=speaker, rel=rel, time_str="", body=text)
        result.append(Message(role=Role.USER, content=[ContentBlock.from_text(tag)]))
    return result


# 会话 scope（DB 原值）→ build_inner_context 要的 chat_type 的映射。direct 私聊渲染成
# 飞书 p2p 场景、group 群聊渲染成飞书群场景（_scene_section 群分支）。scope 是 DB 原值
# （common_conversation.scope），chat_type 是 inner_context 的内部口径，两者钉死映射、
# 不让上游直接传 chat_type（上游只知道 scope）。
_SCOPE_TO_CHAT_TYPE = {"direct": "p2p", "group": "group"}


async def build_proactive_chat_context(
    *,
    intent: str,
    persona_id: str,
    chat_id: str,
    user_id: str | None,
    chat_scope: str = "direct",
    chat_name: str = "",
    channel: str | None = None,
    limit: int = 10,
    since: str | None = None,
) -> ChatTurnContext:
    """Build a render-ready context for a *proactive* (life → 真人 / 群) chat turn.

    ``intent`` is what life decided to reach out about (the *what*); the render
    layer turns it into her actual outbound wording (the *how*). History is
    fetched by ``chat_id`` only — no source message. Returns a
    :class:`ChatTurnContext` packed with the LLM messages (history + the intent
    framing message), the resolved persona bundle, and the assembled
    inner_context. There is no "not found" None case: even a first-ever proactive
    (no history) still builds a context carrying just the intent framing message.

    ``chat_scope`` 是会话的 DB 原值（``direct`` 私聊 / ``group`` 群聊），内部映射成
    ``build_inner_context`` 要的 ``chat_type``（direct→p2p、group→group）；群场景把
    ``chat_name`` 群名传下去，让她的渲染上下文是「在群聊『X』里说话」而不是私聊
    （_scene_section 群分支）。默认 ``direct`` 让私聊路径行为不变。

    ``since`` 是 proactive 历史增量水位（= 本轮 life 进入时的 ``LifeState.observed_at``，
    可能 None）：透传给 ``find_recent_chat_messages``，只取水位之后真人新发的消息——治
    她每轮全量拉旧话、对着早就说过的反复主动开口。``since`` 之后没有任何消息 → history
    为空 → ``messages`` 只剩下面那条 intent 框架消息（她这次纯凭意图 + life 状态主动发，
    不揪旧对话）。``since=None``（冷启 / 不带水位）退回原全量最近 ``limit`` 行为。
    """
    history = await find_recent_chat_messages(chat_id=chat_id, limit=limit, since=since)

    messages = await _history_messages(history, persona_id=persona_id)
    # 意图作为最后一条 user 框架消息：它是这次主动开口的触发，驱动渲染产出她的出站话。
    messages.append(
        Message(
            role=Role.USER,
            content=[
                ContentBlock.from_text(f"{_INTENT_HEADER}{intent}{_INTENT_FOOTER}")
            ],
        )
    )

    persona = await load_persona(persona_id)

    # 真人的显示名（拼 inner_context 的场景 / 关系页用）；解析不到退回 None，inner
    # 各段自有缺席兜底。名字解析失败只 log、不挡 context（对称 chat_node 回灌）。
    trigger_username: str | None = None
    if user_id:
        try:
            trigger_username = await find_username(user_id)
        except Exception as e:  # noqa: BLE001 — 名字解析失败退回 None，不挡 context
            logger.warning("[%s] proactive: resolve username %s failed: %s",
                           persona_id, user_id, e)

    # 拼 inner_context（场景 + 生活状态 + 关系/昨天页 + 本子）。chat_type 由 chat_scope
    # 映射（direct→p2p 私聊场景 / group→group 群场景）；群场景把群名传下去（_scene_section
    # 群分支呈现「在群聊『X』里打字」）。未知 scope 兜底成 p2p（私聊是最保守的场景，不会
    # 误把私聊渲染成群）。拼装失败只 log、退回空串——inner_context 不能塌，渲染仍要能用
    # 更薄的 prompt 跑（对称 build_human_chat_context）。
    chat_type = _SCOPE_TO_CHAT_TYPE.get(chat_scope, "p2p")
    inner_context = ""
    try:
        inner_context = await build_inner_context(
            chat_id=chat_id,
            chat_type=chat_type,
            user_ids=[user_id] if user_id else [],
            trigger_user_id=user_id,
            trigger_username=trigger_username,
            persona_id=persona_id,
            chat_name=chat_name,
            channel=channel,
        )
    except Exception as e:  # noqa: BLE001 — inner 拼装失败退回空串，不挡 context
        logger.error("proactive: failed to build inner context: %s", e)

    return ChatTurnContext(
        messages=messages,
        image_registry=None,
        chat_id=chat_id,
        persona_id=persona_id,
        identity=persona.persona_lite,
        appearance=persona.appearance_detail,
        inner_context=inner_context,
        reply_style=persona.default_reply_style,
        persona=persona,
    )
