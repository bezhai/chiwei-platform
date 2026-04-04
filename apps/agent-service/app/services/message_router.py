"""消息路由器 — 决定哪些 persona 应回复某条消息

Phase 2: 只做 @ 路由。
Phase 3 扩展点: 无 @ 通用判断器、主动发言路由。
"""

import logging

logger = logging.getLogger(__name__)


async def _resolve_persona_id(bot_name: str) -> str:
    """从 bot_config 表查 persona_id（复用 bot_context 的逻辑）"""
    from app.services.bot_context import _resolve_persona_id as _resolve
    return await _resolve(bot_name)


class MessageRouter:
    """消息路由决策器"""

    async def route(
        self,
        chat_id: str,
        mentions: list[str],
        bot_name: str,
        is_p2p: bool,
    ) -> list[str]:
        """返回需要回复的 persona_id 列表。

        Args:
            chat_id: 会话 ID
            mentions: 消息中 @mention 的 bot app_id 列表
            bot_name: 发送 MQ 消息的 bot（抢到锁的那个）
            is_p2p: 是否私聊

        Returns:
            persona_id 列表，空列表表示不回复
        """
        if is_p2p:
            pid = await _resolve_persona_id(bot_name)
            logger.info("P2P route: bot_name=%s → persona_id=%s", bot_name, pid)
            return [pid]

        if mentions:
            persona_ids = await self._resolve_mentioned_personas(mentions)
            logger.info(
                "Group @mention route: mentions=%s → persona_ids=%s",
                mentions, persona_ids,
            )
            return persona_ids

        # 群聊无 @ → 不回复（Phase 3 扩展点）
        return []

    async def _resolve_mentioned_personas(
        self, mentions: list[str]
    ) -> list[str]:
        """将 mention 的 app_id 列表映射到 persona_id 列表

        查询 bot_config 表：mention 的 app_id 匹配 app_id 列。
        """
        from app.orm.base import AsyncSessionLocal
        from sqlalchemy import text

        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text(
                    "SELECT DISTINCT persona_id FROM bot_config "
                    "WHERE app_id = ANY(:mentions) "
                    "AND is_active = true "
                    "AND persona_id IS NOT NULL"
                ),
                {"mentions": mentions},
            )
            return [row[0] for row in result.fetchall()]
