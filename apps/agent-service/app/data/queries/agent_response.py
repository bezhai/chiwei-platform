"""agent_responses table queries — chat_request completion + safety status.

Operates on tables: ``agent_responses`` (raw SQL),
``ConversationMessage`` (proactive completion check joins here).
"""
from __future__ import annotations

import json

from sqlalchemy import func, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.data.models import ConversationMessage

__all__ = [
    "set_agent_response_bot",
    "is_chat_request_completed",
    "get_safety_status",
    "set_safety_status",
]


async def set_agent_response_bot(
    session: AsyncSession,
    session_id: str,
    bot_name: str,
    persona_id: str,
) -> None:
    """Update bot_name and persona_id on agent_responses row."""
    await session.execute(
        text(
            "UPDATE agent_responses SET bot_name = :bn, persona_id = :pid "
            "WHERE session_id = :sid"
        ),
        {"bn": bot_name, "pid": persona_id, "sid": session_id},
    )


async def is_chat_request_completed(
    session: AsyncSession,
    session_id: str | None,
    *,
    is_proactive: bool = False,
) -> bool:
    """Return whether a chat_request redelivery should be treated as done."""
    if not session_id:
        return False

    if is_proactive:
        result = await session.execute(
            select(func.count())
            .select_from(ConversationMessage)
            .where(ConversationMessage.response_id == session_id)
            .where(ConversationMessage.role == "assistant")
        )
        return (result.scalar_one() or 0) > 0

    result = await session.execute(
        text("SELECT status FROM agent_responses WHERE session_id = :sid"),
        {"sid": session_id},
    )
    status = result.scalar_one_or_none()
    return status in ("completed", "recalled")


async def get_safety_status(
    session: AsyncSession, session_id: str
) -> str | None:
    """Read ``safety_status`` from ``agent_responses``; None if row missing.

    Phase 2 ``run_post_safety`` 节点入口判 None 时 raise（让 durable
    handler 进 DLQ）—— None 不再被当成 fail-open 的 pending 处理，
    见 spec §3.8 / §4.4。
    """
    result = await session.execute(
        text("SELECT safety_status FROM agent_responses WHERE session_id = :sid"),
        {"sid": session_id},
    )
    return result.scalar_one_or_none()


async def set_safety_status(
    session: AsyncSession,
    session_id: str,
    status: str,
    result_json: dict | None = None,
) -> None:
    """Update safety_status (and optional result) on agent_responses row."""
    await session.execute(
        text(
            "UPDATE agent_responses "
            "SET safety_status = :status, "
            "    safety_result = CAST(:result AS jsonb), "
            "    updated_at = NOW() "
            "WHERE session_id = :session_id"
        ),
        {
            "status": status,
            "result": (json.dumps(result_json) if result_json is not None else None),
            "session_id": session_id,
        },
    )
