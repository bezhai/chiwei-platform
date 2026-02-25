"""
快速搜索功能 - 基于PostgreSQL的简单实现
"""

from datetime import datetime

from sqlalchemy import select

from app.orm.base import AsyncSessionLocal
from app.orm.models import ConversationMessage, LarkGroupChatInfo, LarkUser


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

    async with AsyncSessionLocal() as session:
        # 1. 获取当前消息信息
        current_msg = await session.scalar(
            select(ConversationMessage).where(
                ConversationMessage.message_id == message_id
            )
        )

        if not current_msg:
            return []

        # 2. 获取同一root_message_id的所有消息，left join lark_user表获取用户名
        root_result = await session.execute(
            select(
                ConversationMessage,
                LarkUser.name.label("username"),
                LarkGroupChatInfo.name.label("chat_name"),
            )
            .outerjoin(LarkUser, ConversationMessage.user_id == LarkUser.union_id)
            .outerjoin(
                LarkGroupChatInfo,
                ConversationMessage.chat_id == LarkGroupChatInfo.chat_id,
            )
            .where(ConversationMessage.root_message_id == current_msg.root_message_id)
            .where(ConversationMessage.create_time <= current_msg.create_time)
            .order_by(ConversationMessage.create_time.asc())
        )
        root_rows = root_result.all()
        root_messages: list[tuple[ConversationMessage, str | None, str | None]] = [
            (row[0], row[1], row[2]) for row in root_rows
        ]

        # 3. 如果数量不足，补充同一chat_id的其他消息
        if len(root_messages) < limit:
            needed = limit - len(root_messages)

            # 计算时间窗口
            time_threshold = current_msg.create_time - (time_window_minutes * 60 * 1000)

            additional_result = await session.execute(
                select(
                    ConversationMessage,
                    LarkUser.name.label("username"),
                    LarkGroupChatInfo.name.label("chat_name"),
                )
                .outerjoin(LarkUser, ConversationMessage.user_id == LarkUser.union_id)
                .outerjoin(
                    LarkGroupChatInfo,
                    ConversationMessage.chat_id == LarkGroupChatInfo.chat_id,
                )
                .where(
                    ConversationMessage.chat_id == current_msg.chat_id,
                    ConversationMessage.root_message_id != current_msg.root_message_id,
                    ConversationMessage.create_time >= time_threshold,
                    ConversationMessage.create_time < current_msg.create_time,
                )
                .order_by(ConversationMessage.create_time.desc())
                .limit(needed)
            )
            additional_rows = additional_result.all()
            additional_messages = [(row[0], row[1], row[2]) for row in additional_rows]

            # 合并并排序
            all_messages = root_messages + additional_messages
            all_messages.sort(key=lambda x: x[0].create_time)
        else:
            all_messages = root_messages

        # 4. 转换为搜索结果格式
        results = []
        for msg, username, chat_name in all_messages:
            results.append(
                QuickSearchResult(
                    message_id=str(msg.message_id),
                    content=str(msg.content),
                    user_id=str(msg.user_id),
                    create_time=datetime.fromtimestamp(msg.create_time / 1000),
                    role=str(msg.role),
                    username=username if msg.role == "user" else "赤尾",
                    chat_type=str(msg.chat_type),
                    chat_name=chat_name,
                    reply_message_id=(
                        str(msg.reply_message_id) if msg.reply_message_id else None
                    ),
                    chat_id=msg.chat_id,
                )
            )

        return results
