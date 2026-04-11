"""统一 Voice cron — 替代 monologue_worker + base_style_worker"""

import logging

from app.config.config import settings
from app.orm.crud import get_all_persona_ids
from app.services.voice_generator import generate_voice
from app.workers.error_handling import cron_error_handler

logger = logging.getLogger(__name__)


@cron_error_handler()
async def cron_generate_voice(ctx) -> None:
    """定时生成所有 persona 的统一 voice"""
    if settings.lane and settings.lane != "prod":
        return

    persona_ids = await get_all_persona_ids()
    for pid in persona_ids:
        try:
            await generate_voice(pid, source="cron")
        except Exception as e:
            logger.error(f"[{pid}] Voice generation failed: {e}")
