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
from app.chat.content_parser import parse_content
from app.chat.render import ChatTurnContext
from app.data.queries import find_recent_chat_messages, find_username
from app.memory._persona import load_persona
from app.memory.context import build_inner_context

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


def _history_messages(
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
        role = Role.ASSISTANT if is_self else Role.USER
        result.append(Message(role=role, content=[ContentBlock.from_text(text)]))
    return result


async def build_proactive_chat_context(
    *,
    intent: str,
    persona_id: str,
    chat_id: str,
    user_id: str | None,
    limit: int = 10,
) -> ChatTurnContext:
    """Build a render-ready context for a *proactive* (life → 真人) chat turn.

    ``intent`` is what life decided to reach out about (the *what*); the render
    layer turns it into her actual outbound wording (the *how*). History is
    fetched by ``chat_id`` only — no source message. Returns a
    :class:`ChatTurnContext` packed with the LLM messages (history + the intent
    framing message), the resolved persona bundle, and the assembled
    inner_context. There is no "not found" None case: even a first-ever proactive
    (no history) still builds a context carrying just the intent framing message.
    """
    history = await find_recent_chat_messages(chat_id=chat_id, limit=limit)

    messages = _history_messages(history, persona_id=persona_id)
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

    # 拼 inner_context（场景 + 生活状态 + 关系/昨天页 + 本子）。proactive 永远是飞书
    # 私聊主动发 → chat_type="p2p"，trigger_user/username 指向这个真人。拼装失败只
    # log、退回空串——inner_context 不能塌，渲染仍要能用更薄的 prompt 跑（对称
    # build_human_chat_context）。
    inner_context = ""
    try:
        inner_context = await build_inner_context(
            chat_id=chat_id,
            chat_type="p2p",
            user_ids=[user_id] if user_id else [],
            trigger_user_id=user_id,
            trigger_username=trigger_username,
            persona_id=persona_id,
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
        persona=persona,
    )
