"""Reply-style queries — reply_style_log.

Operates on table: ``ReplyStyleLog``.

旧 life_engine_state / glimpse_state 的查询已随 world/life 重写删除——她此刻的
主观状态现在读新的 ``LifeState`` Data（``app.domain.life_state``）。这里只剩
voice 写、chat 读的回应语气（reply style）审计日志这一对。
"""
from __future__ import annotations

from sqlalchemy.future import select

from app.data.models import ReplyStyleLog
from app.runtime.db import auto_tx, current_session

__all__ = [
    "insert_reply_style",
    "find_latest_reply_style",
]


async def insert_reply_style(
    *,
    persona_id: str,
    style_text: str,
    source: str,
    observation: str | None = None,
) -> None:
    """Append a reply style audit log entry (written by voice generation)."""
    async with auto_tx():
        current_session().add(
            ReplyStyleLog(
                persona_id=persona_id,
                style_text=style_text,
                source=source,
                observation=observation,
            )
        )


async def find_latest_reply_style(persona_id: str) -> str | None:
    """Fetch the most recent reply style text for a persona (read by chat)."""
    async with auto_tx():
        result = await current_session().execute(
            select(ReplyStyleLog.style_text)
            .where(ReplyStyleLog.persona_id == persona_id)
            .order_by(ReplyStyleLog.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
