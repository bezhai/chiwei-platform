"""Internal timeline formatter for memory pipelines.

Formats a list of ``ConversationMessage`` into ``[HH:MM] speaker: content``
text.  Used by afterthought, drift, and relationship extraction.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timezone

from app.data.queries import find_username
from app.data.session import get_session
from app.services.content_parser import parse_content


async def _default_resolve_name(user_id: str) -> str | None:
    """Look up display name via data layer."""
    async with get_session() as s:
        return await find_username(s, user_id)


async def format_timeline(
    messages: list,
    persona_name: str,
    *,
    tz: timezone | None = None,
    max_messages: int | None = None,
    with_ids: bool = False,
    username_resolver: Callable | None = None,
) -> str:
    """Format message list as a timestamped timeline string.

    Format: ``[HH:MM] speaker: content`` (truncated to 200 chars).
    ``with_ids=True``: ``#id [HH:MM] speaker: content``.
    """
    if tz is None:
        tz = UTC

    if max_messages is not None:
        messages = messages[-max_messages:]

    resolve_name = username_resolver or _default_resolve_name

    lines: list[str] = []
    for msg in messages:
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=tz)
        time_str = msg_time.strftime("%H:%M")

        if msg.role == "assistant":
            speaker = persona_name
        else:
            name = await resolve_name(msg.user_id)
            speaker = name or msg.user_id[:6]

        rendered = parse_content(msg.content).render()
        if rendered and rendered.strip():
            prefix = f"#{msg.id} " if with_ids and msg.id else ""
            lines.append(f"{prefix}[{time_str}] {speaker}: {rendered[:200]}")

    return "\n".join(lines)
