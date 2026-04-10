"""PersonaLoader — 加载 persona 上下文，带进程级 TTL 缓存

消除 5 处 inline get_bot_persona → extract display_name/persona_lite 的重复模式。
"""

import time
from dataclasses import dataclass

from app.orm.crud import get_bot_persona

# 缓存 TTL（秒）
_CACHE_TTL = 300  # 5 min

# 进程级缓存: persona_id -> (PersonaContext, expire_ts)
_persona_cache: dict[str, tuple["PersonaContext", float]] = {}


@dataclass(frozen=True)
class PersonaContext:
    persona_id: str
    display_name: str
    persona_lite: str
    bot_name: str | None = None


async def load_persona(persona_id: str) -> PersonaContext:
    """Load persona context with process-level TTL cache (5 min)"""
    now = time.monotonic()

    cached = _persona_cache.get(persona_id)
    if cached is not None:
        ctx, expire_ts = cached
        if now < expire_ts:
            return ctx

    persona = await get_bot_persona(persona_id)
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
