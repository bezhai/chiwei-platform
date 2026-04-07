"""Life Engine cron wrapper — 每分钟 tick 所有 persona"""

import logging

from app.config.config import settings
from app.services.life_engine import LifeEngine

logger = logging.getLogger(__name__)

_engine = LifeEngine()


async def cron_life_engine_tick(ctx) -> None:
    """arq cron: 每分钟为每个 persona 执行一次 tick

    非 prod 泳道跳过，避免与 prod 写同一张表冲突。
    泳道测试请用 POST /admin/trigger-life-engine-tick（dry_run）。
    """
    if settings.lane and settings.lane != "prod":
        return

    from app.orm.crud import get_all_persona_ids

    persona_ids = await get_all_persona_ids()
    for persona_id in persona_ids:
        try:
            await _engine.tick(persona_id)
        except Exception as e:
            logger.error(f"[{persona_id}] Life engine tick failed: {e}")
