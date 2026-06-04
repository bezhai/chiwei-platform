"""
快速搜索功能 - 基于PostgreSQL的简单实现
"""

from datetime import datetime

from app.data.queries import (
    find_message_by_id,
    find_messages_with_user_chat_persona_by_root,
    find_messages_with_user_chat_persona_in_chat,
)

# 主动触发消息的合成 user_id 标记。归属在这条 chat 路径的叶子模块，
# context.py 再 import 它（避免 context ↔ quick_search 循环 import）。
PROACTIVE_USER_ID = "__proactive__"


class QuickSearchResult:
    """搜索结果项"""

    def __init__(
        self,
        message_id: str,
        content: str,
        user_id: str,
        create_time: datetime,
        role: str,
        username: str | None = None,
        chat_type: str | None = None,
        chat_name: str | None = None,
        reply_message_id: str | None = None,
        chat_id: str | None = None,
        bot_name: str | None = None,
        persona_id: str | None = None,
    ):
        self.message_id = message_id
        self.content = content
        self.user_id = user_id
        self.create_time = create_time
        self.role = role
        self.username = username
        self.chat_type = chat_type
        self.chat_name = chat_name
        self.reply_message_id = reply_message_id
        self.chat_id = chat_id
        self.bot_name = bot_name
        self.persona_id = persona_id


async def quick_search(
    message_id: str, limit: int = 15, time_window_minutes: int = 30
) -> list[QuickSearchResult]:
    """
    快速搜索相关消息 - 基于消息ID获取相关对话历史

    逻辑:
    1. 首先找到当前消息的root_message_id
    2. 获取同一root下的所有消息
    3. 如果数量不足，补充同一chat_id下最近time_window_minutes分钟的消息

    Args:
        message_id: 起始消息ID
        limit: 返回消息数量限制
        time_window_minutes: 补充消息的时间窗口（分钟）

    Returns:
        List[QuickSearchResult]: 搜索结果列表，按时间排序
    """
    current_msg = await find_message_by_id(message_id)
    if not current_msg:
        return []

    root_messages = await find_messages_with_user_chat_persona_by_root(
        root_message_id=current_msg.root_message_id,
        until_create_time=current_msg.create_time,
    )

    # Truncate root chain to limit (keep most recent, trigger message last)
    if len(root_messages) > limit:
        root_messages = root_messages[-limit:]

    if len(root_messages) < limit:
        needed = limit - len(root_messages)
        time_threshold = current_msg.create_time - (time_window_minutes * 60 * 1000)

        additional_messages = await find_messages_with_user_chat_persona_in_chat(
            chat_id=current_msg.chat_id,
            exclude_root_message_id=current_msg.root_message_id,
            after_create_time=time_threshold,
            before_create_time=current_msg.create_time,
            exclude_user_id=PROACTIVE_USER_ID,
            limit=needed,
        )

        all_messages = root_messages + additional_messages
        all_messages.sort(key=lambda x: x[0].create_time)
    else:
        all_messages = root_messages

    results = []
    for msg, username, chat_name, persona_id in all_messages:
        results.append(
            QuickSearchResult(
                message_id=str(msg.message_id),
                content=str(msg.content),
                user_id=str(msg.user_id),
                create_time=datetime.fromtimestamp(msg.create_time / 1000),
                role=str(msg.role),
                username=username
                if msg.role == "user"
                else (msg.bot_name or "assistant"),
                bot_name=msg.bot_name if msg.role == "assistant" else None,
                persona_id=persona_id if msg.role == "assistant" else None,
                chat_type=str(msg.chat_type),
                chat_name=chat_name,
                reply_message_id=(
                    str(msg.reply_message_id) if msg.reply_message_id else None
                ),
                chat_id=msg.chat_id,
            )
        )

    return results
