"""内心独白 cron worker"""

import logging

from app.config.config import settings

logger = logging.getLogger(__name__)


async def cron_generate_inner_monologue(ctx) -> None:
    """arq cron: 为每个 persona 生成内心独白"""
    if settings.lane and settings.lane != "prod":
        return

    from app.orm.crud import get_all_persona_ids
    from app.services.inner_monologue import generate_inner_monologue

    for persona_id in await get_all_persona_ids():
        try:
            result = await generate_inner_monologue(persona_id)
            if result:
                logger.info(f"[{persona_id}] Inner monologue generated: {len(result)} chars")
        except Exception as e:
            logger.error(f"[{persona_id}] Inner monologue generation failed: {e}", exc_info=True)
