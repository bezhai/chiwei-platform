"""Cross-chat context builder.

Fetches recent interactions between a user and a persona across different chats,
formats them for injection into the system prompt.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from inner_shared.dynamic_config import dynamic_config

from app.chat.content_parser import parse_content
from app.data.models import ConversationMessage
from app.data.queries import (
    find_bot_names_for_persona,
    find_cross_chat_messages,
    find_group_name,
)
from app.data.session import get_session

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))

_24H_MS = 24 * 60 * 60 * 1000
_MAX_PAIRS_PER_CHAT = 5
DEFAULT_MAX_TOTAL_MESSAGES = 15


def _excluded_chats() -> list[str]:
    try:
        raw = dynamic_config.get("memory.cross_chat.excluded_chat_ids", default="")
    except Exception:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _max_total_messages() -> int:
    try:
        return dynamic_config.get_int(
            "memory.cross_chat.max_total_messages",
            default=DEFAULT_MAX_TOTAL_MESSAGES,
        )
    except Exception:
        return DEFAULT_MAX_TOTAL_MESSAGES


def _filter_direct_interactions(
    messages: list[ConversationMessage],
    user_id: str,
) -> list[ConversationMessage]:
    """Keep only trigger user's messages and assistant replies to those messages."""
    user_message_ids = {
        msg.message_id
        for msg in messages
        if msg.role == "user" and msg.user_id == user_id
    }

    return [
        msg
        for msg in messages
        if (msg.role == "user" and msg.user_id == user_id)
        or (msg.role == "assistant" and msg.reply_message_id in user_message_ids)
    ]


def _group_and_trim(
    messages: list[ConversationMessage],
    max_pairs_per_chat: int = _MAX_PAIRS_PER_CHAT,
) -> dict[str, list[ConversationMessage]]:
    """Group messages by chat_id, keep last N interaction pairs per chat."""
    by_chat: dict[str, list[ConversationMessage]] = defaultdict(list)
    for msg in messages:
        by_chat[msg.chat_id].append(msg)

    trimmed: dict[str, list[ConversationMessage]] = {}
    for chat_id, chat_msgs in by_chat.items():
        pair_count = sum(1 for m in chat_msgs if m.role == "user")
        if pair_count <= max_pairs_per_chat:
            trimmed[chat_id] = chat_msgs
        else:
            user_msgs = [m for m in chat_msgs if m.role == "user"]
            keep_from = user_msgs[-max_pairs_per_chat].create_time
            trimmed[chat_id] = [m for m in chat_msgs if m.create_time >= keep_from]

    return trimmed


def _render_text(content: str) -> str:
    """Extract plain text from v2 JSON message content."""
    try:
        return parse_content(content).render().strip()
    except Exception:
        return content.strip()


def _relative_time(ts_ms: int) -> str:
    """Format timestamp as relative time string (CST)."""
    now = datetime.now(_CST)
    msg_time = datetime.fromtimestamp(ts_ms / 1000, _CST)
    delta = now - msg_time

    if delta < timedelta(minutes=5):
        return "刚刚"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}分钟前"
    if delta < timedelta(hours=12):
        return f"{int(delta.total_seconds() // 3600)}小时前"
    if msg_time.date() == now.date():
        return f"今天{msg_time.strftime('%H:%M')}"
    if msg_time.date() == (now - timedelta(days=1)).date():
        return f"昨天{msg_time.strftime('%H:%M')}"
    return msg_time.strftime("%m-%d %H:%M")


def _format_interactions(
    grouped: dict[str, list[ConversationMessage]],
    username: str,
    chat_names: dict[str, str],
) -> str:
    """Format grouped cross-chat interactions into readable text."""
    if not grouped:
        return ""

    parts: list[str] = []

    for chat_id, msgs in grouped.items():
        if not msgs:
            continue

        chat_name = chat_names.get(
            chat_id, "私聊" if msgs[0].chat_type == "p2p" else chat_id[:8]
        )
        first_ts = msgs[0].create_time

        lines: list[str] = [f"{chat_name} · {_relative_time(first_ts)}:"]
        for msg in msgs:
            text = _render_text(msg.content)
            if not text:
                continue
            if len(text) > 150:
                text = text[:147] + "..."
            speaker = "你" if msg.role == "assistant" else username
            lines.append(f"  {speaker}: {text}")

        if len(lines) > 1:
            parts.append("\n".join(lines))

    if not parts:
        return ""

    return f"[你和 {username} 最近在其他地方的互动]\n\n" + "\n\n".join(parts)


async def build_cross_chat_context(
    persona_id: str,
    trigger_user_id: str | None,
    trigger_username: str,
    current_chat_id: str,
) -> str:
    """Build the cross-chat interaction section for inner_context.

    Returns empty string if no cross-chat interactions found,
    or if trigger_user_id is None or "__proactive__".
    """
    if not trigger_user_id or trigger_user_id == "__proactive__":
        return ""

    try:
        async with get_session() as s:
            bot_names = await find_bot_names_for_persona(s, persona_id)
        if not bot_names:
            return ""

        now_ms = int(datetime.now(_CST).timestamp() * 1000)
        since_ms = now_ms - _24H_MS

        async with get_session() as s:
            messages = await find_cross_chat_messages(
                s,
                user_id=trigger_user_id,
                bot_names=bot_names,
                exclude_chat_id=current_chat_id,
                since_ms=since_ms,
                excluded_chat_ids=_excluded_chats(),
            )

        max_total = _max_total_messages()
        if max_total > 0:
            messages = messages[:max_total]

        if not messages:
            return ""

        messages = _filter_direct_interactions(messages, trigger_user_id)
        if not messages:
            return ""

        grouped = _group_and_trim(messages)

        chat_names: dict[str, str] = {}
        for chat_id in grouped:
            sample = grouped[chat_id][0]
            if sample.chat_type == "p2p":
                chat_names[chat_id] = "私聊"
            else:
                async with get_session() as s:
                    name = await find_group_name(s, chat_id)
                chat_names[chat_id] = name or chat_id[:8]

        return _format_interactions(grouped, trigger_username, chat_names)

    except Exception as e:
        logger.warning("Failed to build cross-chat context for %s: %s", persona_id, e)
        return ""
