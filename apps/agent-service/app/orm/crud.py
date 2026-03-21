from datetime import UTC, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.future import select

from .base import AsyncSessionLocal
from .models import (
    AkaoJournal,
    AkaoSchedule,
    ChatImpression,
    ConversationMessage,
    DiaryEntry,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    LarkUser,
    ModelMapping,
    ModelProvider,
    PersonImpression,
    WeeklyReview,
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


async def get_active_p2p_chat_ids(
    min_replies: int = 2, days: int = 1
) -> list[str]:
    """查询近 N 天内赤尾回复 >= min_replies 次的私聊 chat_id"""
    async with AsyncSessionLocal() as session:
        cutoff_ms = int(
            (datetime.now(UTC) - timedelta(days=days)).timestamp() * 1000
        )
        result = await session.execute(
            select(ConversationMessage.chat_id)
            .where(ConversationMessage.chat_type == "p2p")
            .where(ConversationMessage.role == "assistant")
            .where(ConversationMessage.create_time > cutoff_ms)
            .group_by(ConversationMessage.chat_id)
            .having(func.count() >= min_replies)
        )
        return [row[0] for row in result.all()]


async def get_cross_group_impressions(
    user_id: str, limit: int = 5
) -> list[tuple[PersonImpression, str]]:
    """查询某用户在所有群聊中的印象（按更新时间倒序）

    JOIN lark_group_chat_info 自然过滤掉 P2P chat_id，
    确保只返回群聊来源的印象。

    Returns:
        list of (PersonImpression, group_name)
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(PersonImpression, LarkGroupChatInfo.name)
            .join(
                LarkGroupChatInfo,
                PersonImpression.chat_id == LarkGroupChatInfo.chat_id,
            )
            .where(PersonImpression.user_id == user_id)
            .order_by(PersonImpression.updated_at.desc())
            .limit(limit)
        )
        return [(row[0], row[1]) for row in result.all()]


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


async def get_diaries_in_range(
    chat_id: str, start_date: str, end_date: str
) -> list[DiaryEntry]:
    """查日期范围内的日记（含首尾）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiaryEntry)
            .where(DiaryEntry.chat_id == chat_id)
            .where(DiaryEntry.diary_date >= start_date)
            .where(DiaryEntry.diary_date <= end_date)
            .order_by(DiaryEntry.diary_date.asc())
        )
        return list(result.scalars().all())


# ==================== WeeklyReview CRUD ====================


async def upsert_weekly_review(
    chat_id: str,
    week_start: str,
    week_end: str,
    content: str,
    model: str | None = None,
) -> None:
    """插入或更新周记（upsert by chat_id + week_start）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(WeeklyReview)
            .where(WeeklyReview.chat_id == chat_id)
            .where(WeeklyReview.week_start == week_start)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.content = content
            existing.week_end = week_end
            existing.model = model
        else:
            session.add(
                WeeklyReview(
                    chat_id=chat_id,
                    week_start=week_start,
                    week_end=week_end,
                    content=content,
                    model=model,
                )
            )
        await session.commit()


async def get_latest_weekly_review(
    chat_id: str, before_date: str, limit: int = 1
) -> list[WeeklyReview]:
    """查最近 N 篇周记（week_start 在 before_date 之前）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(WeeklyReview)
            .where(WeeklyReview.chat_id == chat_id)
            .where(WeeklyReview.week_start < before_date)
            .order_by(WeeklyReview.week_start.desc())
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


# ==================== AkaoSchedule CRUD ====================


_WEEKDAY_MAP = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


async def get_current_schedule(
    now_date: str, now_time: str, weekday: int
) -> list[AkaoSchedule]:
    """查询当前时刻生效的日程条目

    优先级：daily（精确日期的时段）> weekly > monthly
    返回所有匹配的活跃条目，由调用方组装上下文。

    Args:
        now_date: 当前日期 "2026-03-18"
        now_time: 当前时间 "14:30"
        weekday: 星期几 (0=Monday, 6=Sunday)
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.is_active.is_(True))
            .where(AkaoSchedule.period_start <= now_date)
            .where(AkaoSchedule.period_end >= now_date)
            .order_by(AkaoSchedule.plan_type.asc())  # daily < monthly < weekly 字母序
        )
        all_entries = list(result.scalars().all())

    matched: list[AkaoSchedule] = []
    for entry in all_entries:
        if entry.plan_type in ("monthly", "weekly"):
            matched.append(entry)
        elif entry.plan_type == "daily":
            # daily 条目需要匹配 time_start <= now_time < time_end
            if entry.time_start and entry.time_end:
                if entry.time_start <= now_time < entry.time_end:
                    matched.append(entry)
    return matched


async def get_latest_plan(plan_type: str, before_date: str) -> AkaoSchedule | None:
    """查指定类型的最近一条计划（用于生成下一期计划时的上下文）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == plan_type)
            .where(AkaoSchedule.is_active.is_(True))
            .where(AkaoSchedule.period_end < before_date)
            .order_by(AkaoSchedule.period_end.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_plan_for_period(
    plan_type: str, period_start: str, period_end: str
) -> AkaoSchedule | None:
    """查指定周期的计划"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == plan_type)
            .where(AkaoSchedule.period_start == period_start)
            .where(AkaoSchedule.period_end == period_end)
        )
        return result.scalar_one_or_none()


async def get_daily_entries_for_date(target_date: str) -> list[AkaoSchedule]:
    """查指定日期的所有日计划时段"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == "daily")
            .where(AkaoSchedule.period_start == target_date)
            .where(AkaoSchedule.is_active.is_(True))
            .order_by(AkaoSchedule.time_start.asc())
        )
        return list(result.scalars().all())


async def list_schedules(
    plan_type: str | None = None,
    active_only: bool = True,
    limit: int = 50,
) -> list[AkaoSchedule]:
    """列出日程条目"""
    async with AsyncSessionLocal() as session:
        stmt = select(AkaoSchedule)
        if plan_type:
            stmt = stmt.where(AkaoSchedule.plan_type == plan_type)
        if active_only:
            stmt = stmt.where(AkaoSchedule.is_active.is_(True))
        stmt = stmt.order_by(
            AkaoSchedule.period_start.desc(), AkaoSchedule.time_start.asc()
        ).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def upsert_schedule(entry: AkaoSchedule) -> AkaoSchedule:
    """插入或更新日程条目（按 unique key 匹配）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == entry.plan_type)
            .where(AkaoSchedule.period_start == entry.period_start)
            .where(AkaoSchedule.period_end == entry.period_end)
            .where(
                AkaoSchedule.time_start == entry.time_start
                if entry.time_start
                else AkaoSchedule.time_start.is_(None)
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.content = entry.content
            existing.mood = entry.mood
            existing.energy_level = entry.energy_level
            existing.response_style_hint = entry.response_style_hint
            existing.proactive_action = entry.proactive_action
            existing.target_chats = entry.target_chats
            existing.model = entry.model
            existing.is_active = entry.is_active
            await session.commit()
            await session.refresh(existing)
            return existing
        else:
            session.add(entry)
            await session.commit()
            await session.refresh(entry)
            return entry


async def delete_schedule(schedule_id: int) -> bool:
    """删除日程条目"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule).where(AkaoSchedule.id == schedule_id)
        )
        entry = result.scalar_one_or_none()
        if not entry:
            return False
        await session.delete(entry)
        await session.commit()
        return True


# ==================== AkaoJournal CRUD ====================


async def upsert_journal(
    journal_type: str,
    journal_date: str,
    period_end: str,
    content: str,
    source_chat_count: int = 0,
    model: str | None = None,
) -> None:
    """插入或更新日志（upsert by journal_type + journal_date）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoJournal)
            .where(AkaoJournal.journal_type == journal_type)
            .where(AkaoJournal.journal_date == journal_date)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.content = content
            existing.period_end = period_end
            existing.source_chat_count = source_chat_count
            existing.model = model
        else:
            session.add(
                AkaoJournal(
                    journal_type=journal_type,
                    journal_date=journal_date,
                    period_end=period_end,
                    content=content,
                    source_chat_count=source_chat_count,
                    model=model,
                )
            )
        await session.commit()


async def get_recent_journals(
    journal_type: str, before_date: str, limit: int = 2
) -> list[AkaoJournal]:
    """查最近 N 篇日志（before_date 之前）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoJournal)
            .where(AkaoJournal.journal_type == journal_type)
            .where(AkaoJournal.journal_date < before_date)
            .order_by(AkaoJournal.journal_date.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def get_journal_for_date(
    journal_type: str, journal_date: str
) -> AkaoJournal | None:
    """查指定日期的日志"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoJournal)
            .where(AkaoJournal.journal_type == journal_type)
            .where(AkaoJournal.journal_date == journal_date)
        )
        return result.scalar_one_or_none()


async def get_journals_in_range(
    journal_type: str, start_date: str, end_date: str
) -> list[AkaoJournal]:
    """查日期范围内的日志（含首尾）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoJournal)
            .where(AkaoJournal.journal_type == journal_type)
            .where(AkaoJournal.journal_date >= start_date)
            .where(AkaoJournal.journal_date <= end_date)
            .order_by(AkaoJournal.journal_date.asc())
        )
        return list(result.scalars().all())


async def get_all_diaries_for_date(diary_date: str) -> list[DiaryEntry]:
    """查询所有聊天在指定日期的日记（日志合成的素材）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiaryEntry)
            .where(DiaryEntry.diary_date == diary_date)
            .order_by(DiaryEntry.id.asc())
        )
        return list(result.scalars().all())


# ==================== ChatImpression CRUD ====================


async def upsert_chat_impression(
    chat_id: str, impression_text: str, model: str | None = None
) -> None:
    """插入或更新群氛围印象"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChatImpression)
            .where(ChatImpression.chat_id == chat_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.impression_text = impression_text
            existing.model = model
        else:
            session.add(
                ChatImpression(
                    chat_id=chat_id,
                    impression_text=impression_text,
                    model=model,
                )
            )
        await session.commit()


async def get_chat_impression(chat_id: str) -> ChatImpression | None:
    """查指定聊天的氛围印象"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ChatImpression)
            .where(ChatImpression.chat_id == chat_id)
        )
        return result.scalar_one_or_none()
