from sqlalchemy.future import select

from .base import AsyncSessionLocal
from .models import (
    AkaoSchedule,
    ConversationMessage,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    LarkUser,
    ModelMapping,
    ModelProvider,
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
            "use_proxy": provider.use_proxy,
        }


async def get_bot_persona(persona_id: str) -> "BotPersona | None":
    """获取 bot 人设配置"""
    from app.orm.models import BotPersona
    async with AsyncSessionLocal() as session:
        return await session.get(BotPersona, persona_id)


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


async def get_latest_plan(plan_type: str, before_date: str, persona_id: str) -> AkaoSchedule | None:
    """查指定类型的最近一条计划（用于生成下一期计划时的上下文，per-bot）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == plan_type)
            .where(AkaoSchedule.is_active.is_(True))
            .where(AkaoSchedule.period_end < before_date)
            .where(AkaoSchedule.persona_id == persona_id)
            .order_by(AkaoSchedule.period_end.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def get_plan_for_period(
    plan_type: str, period_start: str, period_end: str, persona_id: str
) -> AkaoSchedule | None:
    """查指定周期的计划（per-bot）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == plan_type)
            .where(AkaoSchedule.period_start == period_start)
            .where(AkaoSchedule.period_end == period_end)
            .where(AkaoSchedule.persona_id == persona_id)
        )
        return result.scalar_one_or_none()


async def get_daily_entries_for_date(target_date: str, persona_id: str) -> list[AkaoSchedule]:
    """查指定日期的所有日计划时段（per-bot）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.plan_type == "daily")
            .where(AkaoSchedule.period_start == target_date)
            .where(AkaoSchedule.is_active.is_(True))
            .where(AkaoSchedule.persona_id == persona_id)
            .order_by(AkaoSchedule.time_start.asc())
        )
        return list(result.scalars().all())


async def list_schedules(
    plan_type: str | None = None,
    persona_id: str | None = None,
    active_only: bool = True,
    limit: int = 50,
) -> list[AkaoSchedule]:
    """列出日程条目（可按 persona_id 过滤）"""
    async with AsyncSessionLocal() as session:
        stmt = select(AkaoSchedule)
        if plan_type:
            stmt = stmt.where(AkaoSchedule.plan_type == plan_type)
        if persona_id:
            stmt = stmt.where(AkaoSchedule.persona_id == persona_id)
        if active_only:
            stmt = stmt.where(AkaoSchedule.is_active.is_(True))
        stmt = stmt.order_by(
            AkaoSchedule.period_start.desc(), AkaoSchedule.time_start.asc()
        ).limit(limit)
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def upsert_schedule(entry: AkaoSchedule) -> AkaoSchedule:
    """插入或更新日程条目（按 unique key 匹配，含 persona_id）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoSchedule)
            .where(AkaoSchedule.persona_id == entry.persona_id)
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


async def get_all_persona_ids() -> list[str]:
    """获取所有 persona 的 persona_id 列表"""
    from app.orm.models import BotPersona
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(BotPersona.persona_id))
        return [row[0] for row in result.all()]
