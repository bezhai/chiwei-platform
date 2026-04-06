"""Life Engine cron wrapper — 每分钟 tick 所有 persona"""

import logging

from app.services.life_engine import LifeEngine

logger = logging.getLogger(__name__)

_engine = LifeEngine()


async def cron_life_engine_tick(ctx) -> None:
    """arq cron: 每分钟为每个 persona 执行一次 tick"""
    from app.orm.crud import get_all_persona_ids

    persona_ids = await get_all_persona_ids()
    for persona_id in persona_ids:
        try:
            await _engine.tick(persona_id)
        except Exception as e:
            logger.error(f"[{persona_id}] Life engine tick failed: {e}")
