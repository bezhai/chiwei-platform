"""LLM message list builders for chat (private to chat/).

Extracted from chat/context.py per Phase 6 v4 §3.1: keep build_chat_context
orchestrator slim by moving message-shape construction into a focused module.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.prompts import get_prompt
from app.chat.content_parser import parse_content
from app.chat.quick_search import QuickSearchResult

logger = logging.getLogger(__name__)


def image_fn(image_key_to_filename: dict[str, str]) -> Callable[[int, str], str]:
    """Return an image render function for parse_content.render()."""

    def _fn(_i: int, key: str) -> str:
        fn = image_key_to_filename.get(key)
        return f"@{fn}" if fn else "[图片]"

    return _fn


def extract_reply_chain(
    messages: list[QuickSearchResult], trigger_id: str
) -> tuple[list[QuickSearchResult], list[QuickSearchResult]]:
    """Trace reply_message_id chain from trigger upward.

    Returns (chain_messages, other_messages), both in time-ascending order.
    """
    msg_map = {msg.message_id: msg for msg in messages}
    chain_ids: set[str] = set()
    current_id: str | None = trigger_id

    while current_id and current_id in msg_map:
        chain_ids.add(current_id)
        current_id = msg_map[current_id].reply_message_id

    chain = [m for m in messages if m.message_id in chain_ids]
    other = [m for m in messages if m.message_id not in chain_ids]
    return chain, other


def _speaker_of(msg: QuickSearchResult) -> str:
    """群上下文里这条消息的说话人。

    身份全局化删了 lark_user JOIN：assistant 行本就无 username，
    历史 user 行迁移前也全空。assistant 行按 role 派生固定说话人
    （与 history.py 的 check_chat_history / search_group_history 一致：
    用 "我"，不读 username），只有 user 行才 `username or 占位`，
    避免把赤尾历史发言渲染成占位词喂给模型。
    """
    if msg.role == "assistant":
        return "我"
    return msg.username or "未知用户"


def build_group_messages(
    messages: list[QuickSearchResult],
    trigger_id: str,
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
) -> list[HumanMessage | AIMessage]:
    """Build group chat message list.

    Reply chain messages include image content blocks; other messages
    reference images as @N.png in text only.
    """
    chain, other = extract_reply_chain(messages, trigger_id)
    img_fn = image_fn(image_key_to_filename)

    chain_lines = []
    for msg in chain:
        time_str = msg.create_time.strftime("%H:%M:%S")
        speaker = _speaker_of(msg)
        text = parse_content(msg.content).render(image_fn=img_fn)
        marker = " ⭐" if msg.message_id == trigger_id else ""
        chain_lines.append(f"[{time_str}] {speaker}: {text}{marker}")

    other_lines = []
    for msg in other:
        time_str = msg.create_time.strftime("%H:%M:%S")
        speaker = _speaker_of(msg)
        text = parse_content(msg.content).render(image_fn=img_fn)
        other_lines.append(f"[{time_str}] {speaker}: {text}")

    user_content = get_prompt("context_builder").compile(
        reply_chain="\n".join(chain_lines) if chain_lines else "（无回复链）",
        other_messages="\n".join(other_lines) if other_lines else "（无其他消息）",
    )

    content_blocks: list = [{"type": "text", "text": user_content}]

    # Attach reply chain images as content blocks
    for msg in chain:
        parsed = parse_content(msg.content)
        for key in parsed.image_keys:
            fn = image_key_to_filename.get(key)
            url = image_key_to_url.get(key)
            if fn and url:
                content_blocks.append({"type": "text", "text": f"@{fn}:"})
                content_blocks.append({"type": "image", "url": url})

    return [HumanMessage(content_blocks=content_blocks)]  # type: ignore[arg-type]


def build_p2p_messages(
    messages: list[QuickSearchResult],
    image_key_to_url: dict[str, str],
    image_key_to_filename: dict[str, str],
    current_persona_id: str = "",
) -> list[HumanMessage | AIMessage]:
    """Build P2P message list with full image content blocks."""
    result: list[HumanMessage | AIMessage] = []
    img_fn = image_fn(image_key_to_filename)

    for msg in messages:
        parsed = parse_content(msg.content)
        text_content = parsed.render(image_fn=img_fn)

        content_blocks: list = []
        if text_content:
            content_blocks.append({"type": "text", "text": text_content})

        for key in parsed.image_keys:
            fn = image_key_to_filename.get(key)
            url = image_key_to_url.get(key)
            if fn and url:
                content_blocks.append({"type": "text", "text": f"@{fn}:"})
                content_blocks.append({"type": "image", "url": url})
            elif not fn:
                logger.warning(
                    "Image not registered: key=%s, msg=%s", key, msg.message_id
                )

        if not content_blocks:
            continue

        # Current persona's messages -> AIMessage; everything else -> HumanMessage
        msg_persona_id = getattr(msg, "persona_id", None)
        is_self = (
            msg.role == "assistant"
            and bool(msg_persona_id)
            and msg_persona_id == current_persona_id
        )
        if is_self:
            result.append(AIMessage(content_blocks=content_blocks))  # type: ignore[arg-type]
        else:
            result.append(HumanMessage(content_blocks=content_blocks))  # type: ignore[arg-type]

    return result
