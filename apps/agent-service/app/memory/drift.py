"""Identity drift detection — debounced voice regeneration.

Inherits ``DebouncedPipeline``:
  Phase 1: collect events, debounce N s (configurable), force flush at M.
  Phase 2: gather recent messages + bot replies, call unified voice generation.

Each ``(chat_id, persona_id)`` pair is managed independently.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.data.queries import find_messages_in_range, resolve_bot_name_for_persona
from app.data.session import get_session
from app.infra.config import settings
from app.memory._persona import load_persona
from app.memory._timeline import format_timeline
from app.memory.debounce import DebouncedPipeline
from app.services.content_parser import parse_content

logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))


class _Drift(DebouncedPipeline):
    """Two-phase debounced identity drift detector."""

    def __init__(self) -> None:
        super().__init__(
            debounce_seconds=settings.identity_drift_debounce_seconds,
            max_buffer=settings.identity_drift_max_buffer,
        )

    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        await _run_drift(chat_id, persona_id)


drift = _Drift()


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


async def _run_drift(chat_id: str, persona_id: str) -> None:
    """Event-driven drift — call unified voice generation with recent context."""
    pc = await load_persona(persona_id)
    recent_messages = await _recent_timeline(chat_id, persona_name=pc.display_name)
    recent_replies = await _recent_persona_replies(chat_id, persona_id)

    if not recent_messages:
        logger.info("[%s] No recent messages for %s, skip drift", persona_id, chat_id)
        return

    parts: list[str] = []
    if recent_messages:
        parts.append(f"群里刚才发生的事：\n{recent_messages}")
    if recent_replies:
        parts.append(f"你最近的回复：\n{recent_replies}")
    recent_context = "\n\n".join(parts)

    from app.memory.voice import generate_voice

    await generate_voice(persona_id, recent_context=recent_context, source="drift")


async def _recent_timeline(
    chat_id: str, persona_name: str = "bot", max_messages: int = 50
) -> str:
    """Last 1 hour of messages formatted as timeline."""
    start_dt = datetime.now(_CST) - timedelta(hours=1)
    start_ts = int(start_dt.timestamp() * 1000)
    end_ts = int(datetime.now(_CST).timestamp() * 1000)

    async with get_session() as s:
        messages = await find_messages_in_range(s, chat_id, start_ts, end_ts)
    if not messages:
        return ""

    return await format_timeline(
        messages, persona_name, tz=_CST, max_messages=max_messages
    )


async def _recent_persona_replies(
    chat_id: str, persona_id: str, max_replies: int = 10
) -> str:
    """Recent bot replies for drift diagnosis (matched by bot_name)."""
    now = datetime.now(_CST)
    start_ts = int((now - timedelta(hours=2)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    async with get_session() as s:
        messages = await find_messages_in_range(s, chat_id, start_ts, end_ts)
        if not messages:
            return ""
        bot_name = await resolve_bot_name_for_persona(s, persona_id, chat_id)

    persona_msgs = [
        m for m in messages if m.role == "assistant" and m.bot_name == bot_name
    ]
    persona_msgs = persona_msgs[-max_replies:]

    lines: list[str] = []
    for i, msg in enumerate(persona_msgs, 1):
        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            lines.append(f"{i}. {rendered[:200]}")

    return "\n".join(lines)
