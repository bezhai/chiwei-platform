from datetime import UTC, datetime, timedelta

from sqlalchemy import func, text
from sqlalchemy.future import select

from .base import AsyncSessionLocal
from .models import (
    ConversationMessage,
    DiaryEntry,
    LarkBaseChatInfo,
    LarkUser,
    ModelMapping,
    ModelProvider,
    PersonImpression,
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



# ==================== Diary CRUD ====================


async def get_active_diary_chat_ids(
    min_replies: int = 5, days: int = 7
) -> list[str]:
    """查询近 N 天内赤尾回复 >= min_replies 次的群 chat_id"""
    async with AsyncSessionLocal() as session:
        cutoff_ms = int(
            (datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000
        )
        result = await session.execute(
            select(ConversationMessage.chat_id)
            .where(ConversationMessage.chat_type == "group")
            .where(ConversationMessage.role == "assistant")
            .where(ConversationMessage.create_time > cutoff_ms)
            .group_by(ConversationMessage.chat_id)
            .having(func.count() >= min_replies)
        )
        return [row[0] for row in result.all()]


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


async def upsert_diary_entry(
    chat_id: str,
    diary_date: str,
    content: str,
    message_count: int,
    model: str | None = None,
) -> None:
    """插入或更新日记（upsert by chat_id + diary_date）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiaryEntry)
            .where(DiaryEntry.chat_id == chat_id)
            .where(DiaryEntry.diary_date == diary_date)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.content = content
            existing.message_count = message_count
            existing.model = model
        else:
            session.add(
                DiaryEntry(
                    chat_id=chat_id,
                    diary_date=diary_date,
                    content=content,
                    message_count=message_count,
                    model=model,
                )
            )
        await session.commit()


async def get_recent_diaries(
    chat_id: str, before_date: str, limit: int = 3
) -> list[DiaryEntry]:
    """查最近 N 篇日记（before_date 之前）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiaryEntry)
            .where(DiaryEntry.chat_id == chat_id)
            .where(DiaryEntry.diary_date < before_date)
            .order_by(DiaryEntry.diary_date.desc())
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


# ==================== PersonImpression CRUD ====================


async def get_impressions_for_users(
    chat_id: str, user_ids: list[str]
) -> list[PersonImpression]:
    """查询指定群中指定用户的印象"""
    if not user_ids:
        return []
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonImpression)
            .where(PersonImpression.chat_id == chat_id)
            .where(PersonImpression.user_id.in_(user_ids))
        )
        return list(result.scalars().all())


async def get_all_impressions_for_chat(chat_id: str) -> list[PersonImpression]:
    """查询指定群的所有已有印象"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonImpression).where(PersonImpression.chat_id == chat_id)
        )
        return list(result.scalars().all())


async def get_diary_by_date(chat_id: str, diary_date: str) -> DiaryEntry | None:
    """查指定群指定日期的日记"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiaryEntry)
            .where(DiaryEntry.chat_id == chat_id)
            .where(DiaryEntry.diary_date == diary_date)
        )
        return result.scalar_one_or_none()


async def search_user_by_name(name: str) -> list[LarkUser]:
    """按名字模糊查 lark_user"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LarkUser)
            .where(LarkUser.name.ilike(f"%{name}%"))
            .limit(5)
        )
        return list(result.scalars().all())


async def upsert_person_impression(
    chat_id: str, user_id: str, impression_text: str
) -> None:
    """插入或更新人物印象"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonImpression)
            .where(PersonImpression.chat_id == chat_id)
            .where(PersonImpression.user_id == user_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.impression_text = impression_text
        else:
            session.add(
                PersonImpression(
                    chat_id=chat_id,
                    user_id=user_id,
                    impression_text=impression_text,
                )
            )
        await session.commit()
