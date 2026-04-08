"""Glimpse 独立 cron — 每 5 分钟，仅 browsing 状态下执行"""

import logging

from app.config.config import settings
from app.orm.crud import get_all_persona_ids
from app.services.glimpse import run_glimpse

logger = logging.getLogger(__name__)


async def _load_life_engine_state(persona_id: str):
    """查 Life Engine 最新状态"""
    from app.orm.base import AsyncSessionLocal
    from app.orm.memory_models import LifeEngineState
    from sqlalchemy.future import select

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .order_by(LifeEngineState.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def cron_glimpse(ctx) -> None:
    """arq cron: 每 5 分钟为 browsing 状态的 persona 执行 glimpse

    非 prod 泳道跳过，避免与 prod 写同表冲突。
    泳道测试请用 POST /admin/trigger-glimpse。
    """
    if settings.lane and settings.lane != "prod":
        return

    for persona_id in await get_all_persona_ids():
        try:
            state = await _load_life_engine_state(persona_id)
            if not state or state.activity_type != "browsing":
                continue
            await run_glimpse(persona_id)
        except Exception as e:
            logger.error(f"[{persona_id}] Glimpse cron failed: {e}")
