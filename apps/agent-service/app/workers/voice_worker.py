"""统一 Voice cron — 替代 monologue_worker + base_style_worker"""

import logging

from app.config.config import settings
from app.orm.crud import get_all_active_persona_ids
from app.services.voice_generator import generate_voice

logger = logging.getLogger(__name__)


async def cron_generate_voice(ctx) -> None:
    """定时生成所有 persona 的统一 voice"""
    if settings.lane and settings.lane != "prod":
        return

    persona_ids = await get_all_active_persona_ids()
    for pid in persona_ids:
        try:
            await generate_voice(pid, source="cron")
        except Exception as e:
            logger.error(f"[{pid}] Voice generation failed: {e}")
