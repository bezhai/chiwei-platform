"""Persona context loader with process-level TTL cache.

Provides ``load_persona()`` — a cached lookup that returns a frozen
``PersonaContext`` dataclass.  Cache TTL is 5 minutes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.data.queries import find_persona
from app.data.session import get_session

_CACHE_TTL = 300  # seconds


@dataclass(frozen=True)
class PersonaContext:
    persona_id: str
    display_name: str
    persona_lite: str
    bot_name: str | None = None


_persona_cache: dict[str, tuple[PersonaContext, float]] = {}


async def load_persona(persona_id: str) -> PersonaContext:
    """Load persona context with process-level TTL cache (5 min)."""
    now = time.monotonic()

    cached = _persona_cache.get(persona_id)
    if cached is not None:
        ctx, expire_ts = cached
        if now < expire_ts:
            return ctx

    async with get_session() as s:
        persona = await find_persona(s, persona_id)

    if persona:
        ctx = PersonaContext(
            persona_id=persona_id,
            display_name=persona.display_name,
            persona_lite=persona.persona_lite or "",
        )
    else:
        ctx = PersonaContext(
            persona_id=persona_id,
            display_name=persona_id,
            persona_lite="",
        )

    _persona_cache[persona_id] = (ctx, now + _CACHE_TTL)
    return ctx
