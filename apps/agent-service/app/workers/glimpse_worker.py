"""Glimpse 独立 cron — 每 5 分钟，仅 browsing 状态下执行"""

import logging

from app.config.config import settings
from app.orm.crud import get_all_persona_ids
from app.orm.crud.life_engine import load_latest_state
from app.services.glimpse import run_glimpse
from app.workers.error_handling import cron_error_handler

logger = logging.getLogger(__name__)


@cron_error_handler()
async def cron_glimpse(ctx) -> None:
    """arq cron: 每 5 分钟为 browsing 状态的 persona 执行 glimpse

    非 prod 泳道跳过，避免与 prod 写同表冲突。
    泳道测试请用 POST /admin/trigger-glimpse。
    """
    if settings.lane and settings.lane != "prod":
        return

    for persona_id in await get_all_persona_ids():
        try:
            state = await load_latest_state(persona_id)
            if not state or state.activity_type != "browsing":
                continue
            await run_glimpse(persona_id)
        except Exception as e:
            logger.error(f"[{persona_id}] Glimpse cron failed: {e}")
