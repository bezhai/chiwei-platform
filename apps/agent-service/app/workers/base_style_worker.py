"""基线 reply_style 定时生成

每天 8:00、14:00、18:00 基于 Schedule 生成全局基线 reply_style，
让私聊和冷门群不再 fallback 到静态默认值。
"""

import logging

from app.services.identity_drift import generate_base_reply_style

logger = logging.getLogger(__name__)


async def cron_generate_base_reply_style(ctx) -> None:
    """cron 入口：生成基线 reply_style"""
    try:
        result = await generate_base_reply_style()
        if result:
            logger.info(f"Base reply_style generated: {len(result)} chars")
        else:
            logger.info("Base reply_style skipped (no schedule)")
    except Exception as e:
        logger.error(f"Base reply_style generation failed: {e}", exc_info=True)
