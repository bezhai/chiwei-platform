"""消息收集器

收集用户消息并附带上下文窗口（前3后2 + 回复目标），
为画像沉淀提供结构化的输入。
"""

import logging
from collections import defaultdict
from dataclasses import dataclass

from app.orm.crud import (
    get_message_by_id,
    get_surrounding_messages,
    get_user_messages_since,
    get_username,
)
from app.orm.models import ConversationMessage
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)


@dataclass
class MessageWithContext:
    user_message: ConversationMessage
    context_before: list[ConversationMessage]
    context_after: list[ConversationMessage]
    reply_target: ConversationMessage | None
    chat_id: str

    def render(self, user_names: dict[str, str] | None = None) -> str:
        """渲染为 LLM 可读文本

        Args:
            user_names: {user_id: name} 映射，用于显示用户名
        """
        names = user_names or {}
        lines: list[str] = []

        # 上下文（前）
        for msg in self.context_before:
            label = _get_label(msg, names)
            text = _render_msg(msg)
            if text:
                lines.append(f"  [{label}]: {text}")

        # 回复目标
        if self.reply_target:
            label = _get_label(self.reply_target, names)
            text = _render_msg(self.reply_target)
            if text:
                lines.append(f"  [回复 {label}]: {text}")

        # 用户消息（高亮）
        text = _render_msg(self.user_message)
        user_name = names.get(self.user_message.user_id, "用户")
        lines.append(f"  >>> [{user_name}]: {text}")

        # 上下文（后）
        for msg in self.context_after:
            label = _get_label(msg, names)
            text = _render_msg(msg)
            if text:
                lines.append(f"  [{label}]: {text}")

        return "\n".join(lines)


def _get_label(msg: ConversationMessage, names: dict[str, str]) -> str:
    if msg.role == "assistant":
        return "赤尾"
    return names.get(msg.user_id, msg.user_id[:8])


def _render_msg(msg: ConversationMessage) -> str:
    parsed = parse_content(msg.content)
    return parsed.render()


async def collect_user_messages_with_context(
    user_id: str, since_time: int, max_messages: int = 50
) -> tuple[list[MessageWithContext], dict[str, str]]:
    """收集用户消息并附带上下文窗口

    Args:
        user_id: 用户 ID
        since_time: 起始时间戳（create_time）
        max_messages: 最大消息数

    Returns:
        (messages_with_context, user_names) 元组
    """
    # 1. 获取用户消息
    raw_messages = await get_user_messages_since(user_id, since_time, max_messages)
    if not raw_messages:
        return [], {}

    # 2. 按 chat_id 分组，批量查询上下文
    by_chat: dict[str, list[ConversationMessage]] = defaultdict(list)
    for msg in raw_messages:
        by_chat[msg.chat_id].append(msg)

    # 3. 收集所有需要查名字的 user_id
    user_ids: set[str] = {user_id}

    results: list[MessageWithContext] = []

    for chat_id, msgs in by_chat.items():
        for msg in msgs:
            # 获取上下文
            surrounding = await get_surrounding_messages(
                chat_id, msg.create_time, before=3, after=2
            )

            context_before = [m for m in surrounding if m.create_time < msg.create_time]
            context_after = [m for m in surrounding if m.create_time > msg.create_time]

            # 获取回复目标
            reply_target = None
            if msg.reply_message_id:
                reply_target = await get_message_by_id(msg.reply_message_id)

            # 收集 user_ids
            for m in context_before + context_after:
                if m.role != "assistant":
                    user_ids.add(m.user_id)
            if reply_target and reply_target.role != "assistant":
                user_ids.add(reply_target.user_id)

            results.append(
                MessageWithContext(
                    user_message=msg,
                    context_before=context_before,
                    context_after=context_after,
                    reply_target=reply_target,
                    chat_id=chat_id,
                )
            )

    # 4. 批量获取用户名
    user_names: dict[str, str] = {}
    for uid in user_ids:
        name = await get_username(uid)
        if name:
            user_names[uid] = name

    return results, user_names
