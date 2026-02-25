from datetime import UTC, datetime

from sqlalchemy import func, text
from sqlalchemy.future import select

from .base import AsyncSessionLocal
from .models import (
    ConversationMessage,
    LarkBaseChatInfo,
    LarkUser,
    ModelMapping,
    ModelProvider,
    UserKnowledge,
)


def parse_model_id(model_id: str) -> tuple[str, str]:
    """
    解析model_id格式："{供应商名称}:模型原名"

    Args:
        model_id: 格式为"供应商名称:模型原名"的字符串

    Returns:
        tuple: (供应商名称, 模型原名)
    """
    if ":" in model_id:
        provider_name, model_name = model_id.split(":", 1)
        return provider_name.strip(), model_name.strip()
    else:
        # 如果没有/，使用默认供应商302.ai
        return "302.ai", model_id.strip()


async def get_model_and_provider_info(model_id: str):
    """
    根据model_id获取供应商配置和模型名称
    优先查找 ModelMapping，如果未找到则回退到解析逻辑

    Args:
        model_id: 映射别名 或 格式为"供应商名称/模型原名"的字符串

    Returns:
        dict: 包含模型和供应商信息的字典
    """
    async with AsyncSessionLocal() as session:
        # 1. 尝试查找映射
        mapping_result = await session.execute(
            select(ModelMapping).where(ModelMapping.alias == model_id)
        )
        mapping = mapping_result.scalar_one_or_none()

        if mapping:
            provider_name = mapping.provider_name
            actual_model_name = mapping.real_model_name
        else:
            # 2. 回退到解析逻辑
            provider_name, actual_model_name = parse_model_id(model_id)

        # 3. 查询供应商信息
        provider_result = await session.execute(
            select(ModelProvider).where(ModelProvider.name == provider_name)
        )
        provider = provider_result.scalar_one_or_none()

        # 如果找不到指定供应商，尝试使用默认的302.ai
        if not provider:
            provider_result = await session.execute(
                select(ModelProvider).where(ModelProvider.name == "302.ai")
            )
            provider = provider_result.scalar_one_or_none()

        if not provider:
            return None

        return {
            "model_name": actual_model_name,
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "is_active": provider.is_active,
            "client_type": provider.client_type or "openai",
        }


async def get_gray_config(message_id: str) -> dict | None:
    """
    根据 message_id 关联查询所属 chat 的灰度配置
    """
    async with AsyncSessionLocal() as session:
        # 使用 Join 避免 N+1 查询
        stmt = (
            select(LarkBaseChatInfo.gray_config)
            .join(
                ConversationMessage,
                ConversationMessage.chat_id == LarkBaseChatInfo.chat_id,
            )
            .where(ConversationMessage.message_id == message_id)
        )
        # 直接返回配置字段 (dict) 或者 None
        return await session.scalar(stmt)


async def get_message_content(message_id: str) -> str | None:
    """
    根据 message_id 获取消息内容

    Args:
        message_id: 消息ID

    Returns:
        消息内容，如果未找到返回 None
    """
    async with AsyncSessionLocal() as session:
        stmt = select(ConversationMessage.content).where(
            ConversationMessage.message_id == message_id
        )
        return await session.scalar(stmt)


# ==================== UserKnowledge CRUD ====================


async def get_user_knowledge(user_id: str) -> UserKnowledge | None:
    """根据 user_id 获取用户知识"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(UserKnowledge).where(UserKnowledge.user_id == user_id)
        )
        return result.scalar_one_or_none()


async def upsert_user_knowledge(
    user_id: str,
    facts: list,
    personality_note: str | None,
    communication_style: str | None,
    last_consolidation_message_time: int,
) -> None:
    """插入或更新用户知识"""
    async with AsyncSessionLocal() as session:
        existing = await session.get(UserKnowledge, user_id)
        now = datetime.now(UTC)

        if existing is None:
            session.add(
                UserKnowledge(
                    user_id=user_id,
                    facts=facts,
                    personality_note=personality_note,
                    communication_style=communication_style,
                    last_consolidation_at=now,
                    last_consolidation_message_time=last_consolidation_message_time,
                    consolidation_count=1,
                )
            )
        else:
            existing.facts = facts
            if personality_note is not None:
                existing.personality_note = personality_note
            if communication_style is not None:
                existing.communication_style = communication_style
            existing.last_consolidation_at = now
            existing.last_consolidation_message_time = last_consolidation_message_time
            existing.consolidation_count = (existing.consolidation_count or 0) + 1

        await session.commit()


async def advance_consolidation_cursor(
    user_id: str,
    last_consolidation_message_time: int,
) -> None:
    """仅前移沉淀游标，不修改 facts（用于 skip 路径）"""
    async with AsyncSessionLocal() as session:
        existing = await session.get(UserKnowledge, user_id)
        now = datetime.now(UTC)

        if existing is None:
            session.add(
                UserKnowledge(
                    user_id=user_id,
                    facts=[],
                    last_consolidation_at=now,
                    last_consolidation_message_time=last_consolidation_message_time,
                    consolidation_count=0,
                )
            )
        else:
            existing.last_consolidation_at = now
            existing.last_consolidation_message_time = last_consolidation_message_time

        await session.commit()


async def get_user_messages_since(
    user_id: str, since_time: int, limit: int = 200
) -> list[ConversationMessage]:
    """获取用户在所有 chat 中自 since_time 以来的消息（仅用户发的，不含 assistant）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.user_id == user_id)
            .where(ConversationMessage.role == "user")
            .where(ConversationMessage.create_time > since_time)
            .order_by(ConversationMessage.create_time.asc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_surrounding_messages(
    chat_id: str, create_time: int, before: int = 3, after: int = 2
) -> list[ConversationMessage]:
    """获取指定消息周围的上下文消息"""
    async with AsyncSessionLocal() as session:
        # 前 N 条
        before_result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.chat_id == chat_id)
            .where(ConversationMessage.create_time < create_time)
            .order_by(ConversationMessage.create_time.desc())
            .limit(before)
        )
        before_msgs = list(before_result.scalars().all())
        before_msgs.reverse()

        # 后 N 条
        after_result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.chat_id == chat_id)
            .where(ConversationMessage.create_time > create_time)
            .order_by(ConversationMessage.create_time.asc())
            .limit(after)
        )
        after_msgs = list(after_result.scalars().all())

        return before_msgs + after_msgs


async def get_message_by_id(message_id: str) -> ConversationMessage | None:
    """根据 message_id 获取单条消息"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage).where(
                ConversationMessage.message_id == message_id
            )
        )
        return result.scalar_one_or_none()


async def get_active_users_for_consolidation(
    min_messages: int = 10, max_users: int = 50
) -> list[dict]:
    """找出需要画像沉淀的用户

    条件：自上次沉淀以来有 >= min_messages 条新消息的用户
    返回 [{user_id, message_count, since_time}]
    """
    async with AsyncSessionLocal() as session:
        # 子查询：获取每个用户上次沉淀的时间戳
        profile_subq = select(
            UserKnowledge.user_id,
            UserKnowledge.last_consolidation_message_time,
        ).subquery()

        # 统计每个用户自上次沉淀以来的消息数
        stmt = (
            select(
                ConversationMessage.user_id,
                func.count().label("message_count"),
                func.coalesce(profile_subq.c.last_consolidation_message_time, 0).label(
                    "since_time"
                ),
            )
            .outerjoin(
                profile_subq,
                ConversationMessage.user_id == profile_subq.c.user_id,
            )
            .where(ConversationMessage.role == "user")
            .where(
                ConversationMessage.create_time
                > func.coalesce(profile_subq.c.last_consolidation_message_time, 0)
            )
            .group_by(
                ConversationMessage.user_id,
                profile_subq.c.last_consolidation_message_time,
            )
            .having(func.count() >= min_messages)
            .order_by(text("message_count DESC"))
            .limit(max_users)
        )

        result = await session.execute(stmt)
        return [
            {
                "user_id": row.user_id,
                "message_count": row.message_count,
                "since_time": row.since_time,
            }
            for row in result.all()
        ]


async def get_username(user_id: str) -> str | None:
    """从 lark_user 表获取用户名"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LarkUser.name).where(LarkUser.union_id == user_id)
        )
        return result.scalar_one_or_none()
