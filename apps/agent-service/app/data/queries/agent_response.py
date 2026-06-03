"""common_agent_response queries — chat_request completion + safety status."""

from __future__ import annotations

import json

from sqlalchemy import func, text
from sqlalchemy.future import select
from uuid6 import uuid7

from app.data.models import CommonMessage
from app.runtime.db import auto_tx, current_session

__all__ = [
    "create_pending_agent_response",
    "set_agent_response_bot",
    "is_chat_request_completed",
    "get_safety_status",
    "set_safety_status",
]


async def create_pending_agent_response(
    *,
    session_id: str,
    trigger_common_message_id: str,
    common_conversation_id: str,
    bot_name: str | None,
) -> None:
    """Create a pending common_agent_response row for a fan-out ChatRequest."""
    async with auto_tx():
        await current_session().execute(
            text(
                "INSERT INTO common_agent_response "
                "(response_id, session_id, trigger_common_message_id, "
                "common_conversation_id, bot_name, status, safety_status, "
                "response_type, replies, agent_metadata) "
                "VALUES (CAST(:response_id AS uuid), :session_id, "
                "CAST(:trigger_common_message_id AS uuid), "
                "CAST(:common_conversation_id AS uuid), :bot_name, "
                "'pending', 'pending', 'reply', '[]'::jsonb, '{}'::jsonb) "
                "ON CONFLICT (session_id) DO NOTHING"
            ),
            {
                "response_id": str(uuid7()),
                "session_id": session_id,
                "trigger_common_message_id": trigger_common_message_id,
                "common_conversation_id": common_conversation_id,
                "bot_name": bot_name,
            },
        )


async def set_agent_response_bot(
    session_id: str,
    bot_name: str,
    persona_id: str,
) -> None:
    """Update bot_name and persona_id on common_agent_response row."""
    async with auto_tx():
        await current_session().execute(
            text(
                "UPDATE common_agent_response "
                "SET bot_name = :bn, persona_id = :pid, updated_at = NOW() "
                "WHERE session_id = :sid"
            ),
            {"bn": bot_name, "pid": persona_id, "sid": session_id},
        )


async def is_chat_request_completed(
    session_id: str | None,
    *,
    is_proactive: bool = False,
) -> bool:
    """Return whether a chat_request redelivery should be treated as done."""
    if not session_id:
        return False

    async with auto_tx():
        if is_proactive:
            result = await current_session().execute(
                select(func.count())
                .select_from(CommonMessage)
                .where(CommonMessage.response_id == session_id)
                .where(CommonMessage.role == "assistant")
            )
            return (result.scalar_one() or 0) > 0

        result = await current_session().execute(
            text("SELECT status FROM common_agent_response WHERE session_id = :sid"),
            {"sid": session_id},
        )
        status = result.scalar_one_or_none()
        return status in ("completed", "recalled")


async def get_safety_status(session_id: str) -> str | None:
    """Read ``safety_status`` from ``common_agent_response``; None if row missing.

    Phase 2 ``run_post_safety`` 节点入口判 None 时 raise（让 durable
    handler 进 DLQ）—— None 不再被当成 fail-open 的 pending 处理，
    见 spec §3.8 / §4.4。
    """
    async with auto_tx():
        result = await current_session().execute(
            text(
                "SELECT safety_status FROM common_agent_response "
                "WHERE session_id = :sid"
            ),
            {"sid": session_id},
        )
        return result.scalar_one_or_none()


async def set_safety_status(
    session_id: str,
    status: str,
    result_json: dict | None = None,
) -> None:
    """Update safety_status (and optional result) on common_agent_response row."""
    async with auto_tx():
        await current_session().execute(
            text(
                "UPDATE common_agent_response "
                "SET safety_status = :status, "
                "    safety_result = CAST(:result AS jsonb), "
                "    updated_at = NOW() "
                "WHERE session_id = :session_id"
            ),
            {
                "status": status,
                "result": (
                    json.dumps(result_json) if result_json is not None else None
                ),
                "session_id": session_id,
            },
        )
