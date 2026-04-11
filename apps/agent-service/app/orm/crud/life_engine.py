"""Life Engine state CRUD operations"""

from datetime import datetime

from sqlalchemy.future import select

from app.orm.base import AsyncSessionLocal
from app.orm.memory_models import LifeEngineState


async def load_latest_state(persona_id: str) -> LifeEngineState | None:
    """查最新一行状态，不存在返回 None"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .order_by(LifeEngineState.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def save_state(
    persona_id: str,
    current_state: str,
    activity_type: str,
    response_mood: str,
    skip_until: datetime | None,
    reasoning: str | None = None,
) -> None:
    """INSERT 一行新状态"""
    async with AsyncSessionLocal() as session:
        row = LifeEngineState(
            persona_id=persona_id,
            current_state=current_state,
            activity_type=activity_type,
            response_mood=response_mood,
            reasoning=reasoning,
            skip_until=skip_until,
        )
        session.add(row)
        await session.commit()


async def get_today_activity_states(
    persona_id: str, today_start: datetime
) -> list[LifeEngineState]:
    """获取今天的活动轨迹"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .where(LifeEngineState.created_at >= today_start)
            .order_by(LifeEngineState.created_at.asc())
        )
        return list(result.scalars().all())
