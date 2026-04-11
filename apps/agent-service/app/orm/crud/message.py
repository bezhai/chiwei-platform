"""Message / ConversationMessage CRUD operations"""

from sqlalchemy import text, update
from sqlalchemy.future import select

from app.orm.base import AsyncSessionLocal
from app.orm.models import ConversationMessage, LarkGroupChatInfo, LarkUser


async def get_message_content(message_id: str) -> str | None:
    """根据 message_id 获取消息内容"""
    async with AsyncSessionLocal() as session:
        stmt = select(ConversationMessage.content).where(
            ConversationMessage.message_id == message_id
        )
        return await session.scalar(stmt)


async def get_chat_messages_in_range(
    chat_id: str, start_time: int, end_time: int, limit: int = 2000
) -> list[ConversationMessage]:
    """获取指定群在时间范围内的所有消息（user + assistant）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.chat_id == chat_id)
            .where(ConversationMessage.create_time >= start_time)
            .where(ConversationMessage.create_time < end_time)
            .order_by(ConversationMessage.create_time.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_username(user_id: str) -> str | None:
    """从 lark_user 表获取用户名"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LarkUser.name).where(LarkUser.union_id == user_id)
        )
        return result.scalar_one_or_none()


async def get_group_name(chat_id: str) -> str | None:
    """从 lark_group_chat_info 表获取群名"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LarkGroupChatInfo.name).where(LarkGroupChatInfo.chat_id == chat_id)
        )
        return result.scalar_one_or_none()


# ── Extracted from vectorize_worker.py ──


async def get_message_by_id(message_id: str) -> ConversationMessage | None:
    """从数据库获取完整消息对象"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage).where(
                ConversationMessage.message_id == message_id
            )
        )
        return result.scalar_one_or_none()


async def update_vector_status(message_id: str, status: str) -> None:
    """更新消息的向量化状态"""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ConversationMessage)
            .where(ConversationMessage.message_id == message_id)
            .values(vector_status=status)
        )
        await session.commit()


async def scan_pending_messages(cutoff_ts: int, offset: int, limit: int) -> list[str]:
    """扫描 pending 状态的消息 ID 列表"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage.message_id)
            .where(ConversationMessage.vector_status == "pending")
            .where(ConversationMessage.create_time >= cutoff_ts)
            .order_by(ConversationMessage.create_time.desc())
            .offset(offset)
            .limit(limit)
        )
        return [row[0] for row in result.fetchall()]


# ── Extracted from chat_consumer.py ──


async def update_agent_response_bot(
    session_id: str, bot_name: str, persona_id: str
) -> None:
    """更新 agent_responses 表的 bot_name 和 persona_id"""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE agent_responses SET bot_name = :bn, persona_id = :pid "
                "WHERE session_id = :sid"
            ),
            {"bn": bot_name, "pid": persona_id, "sid": session_id},
        )
        await session.commit()


# ── Extracted from post_consumer.py ──


async def update_safety_status(
    session_id: str, status: str, result_json: dict | None = None
) -> None:
    """更新 agent_responses 表的 safety_status"""
    import json as json_mod

    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE agent_responses "
                "SET safety_status = :status, "
                "    safety_result = CAST(:result AS jsonb), "
                "    updated_at = NOW() "
                "WHERE session_id = :session_id"
            ),
            {
                "status": status,
                "result": json_mod.dumps(result_json) if result_json else None,
                "session_id": session_id,
            },
        )
        await session.commit()
