"""Message routing — decide which personas should reply to a message.

Phase 2: @mention routing only.
Phase 3 extension point: no-@ generic judge, proactive scanning.
"""

from __future__ import annotations

import logging

from app.data.queries import resolve_mentioned_personas, resolve_persona_id
from app.data.session import get_session

logger = logging.getLogger(__name__)


class MessageRouter:
    """Decide which persona_ids should reply to a given message."""

    async def route(
        self,
        chat_id: str,
        mentions: list[str],
        bot_name: str,
        is_p2p: bool,
        is_proactive: bool = False,
    ) -> list[str]:
        """Return persona_id list for responders.

        Args:
            chat_id: conversation ID
            mentions: @mentioned bot app_id values
            bot_name: the bot that grabbed the MQ lock
            is_p2p: whether this is a private chat
            is_proactive: whether this is a proactive trigger message

        Returns:
            persona_id list; empty means "don't reply"
        """
        if is_p2p or is_proactive:
            async with get_session() as s:
                pid = await resolve_persona_id(s, bot_name)
            label = "Proactive" if is_proactive else "P2P"
            logger.info("%s route: bot_name=%s -> persona_id=%s", label, bot_name, pid)
            return [pid]

        if mentions:
            async with get_session() as s:
                persona_ids = await resolve_mentioned_personas(s, mentions)
            logger.info(
                "Group @mention route: mentions=%s -> persona_ids=%s",
                mentions,
                persona_ids,
            )
            return persona_ids

        # Group without @ -> no reply (Phase 3 extension point)
        return []
