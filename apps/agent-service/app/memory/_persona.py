"""Persona context loader with process-level TTL cache.

Provides ``load_persona()`` — a cached lookup that returns a frozen
``PersonaContext`` dataclass.  Cache TTL is 5 minutes.

身份正文（persona_lite）的来源（persona 周级慢漂，读侧单点切换）：优先 persona
版本链最新一版（``read_latest_persona_version``，不分来源——owner 盖版即生效），
链空 / 链读失败整体 fallback ``bot_persona`` 主表，行为与切换前字节级不变。其余
字段（display_name / persona_core / appearance_detail / error_messages）永远来自
主表。lane 口径 = ``current_deployment_lane() or "prod"``（与 pages.py 同）。
缓存机制原样：周级漂移 + 5 分钟 TTL，无需任何失效机制。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from app.data.queries import find_persona
from app.life.persona_chain import read_latest_persona_version
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)

_CACHE_TTL = 300  # seconds


@dataclass(frozen=True)
class PersonaContext:
    persona_id: str
    display_name: str
    persona_lite: str
    persona_core: str = ""
    appearance_detail: str = ""
    error_messages: dict = field(default_factory=dict)
    bot_name: str | None = None


_persona_cache: dict[str, tuple[PersonaContext, float]] = {}


async def _load_chain_narrative(persona_id: str) -> str | None:
    """版本链最新一版的身份正文；链空 / 链最新版空白 / 读失败返回 None（fallback 主表）。

    读失败只 log 不抛——persona 注入绝不能塌掉 chat（照 context.py 的页注入
    姿势）。链以主表为 seed 来源，主表行存在时才会被调用。

    链上最新版正文空白时按链空处理：写侧 update_persona 已拦空白落版，这里是
    防御纵深（owner 人工插空等其它写入口）——五个读取方绝不注入空 identity。
    """
    try:
        lane = current_deployment_lane() or "prod"
        latest = await read_latest_persona_version(
            lane=lane, persona_id=persona_id
        )
    except Exception as e:
        logger.warning(
            "[%s] Failed to read persona version chain: %s", persona_id, e
        )
        return None
    if latest is None:
        return None
    if not latest.narrative or not latest.narrative.strip():
        logger.warning(
            "[%s] Latest persona version narrative is blank, "
            "falling back to bot_persona main table",
            persona_id,
        )
        return None
    return latest.narrative


async def load_persona(persona_id: str) -> PersonaContext:
    """Load persona context with process-level TTL cache (5 min)."""
    now = time.monotonic()

    cached = _persona_cache.get(persona_id)
    if cached is not None:
        ctx, expire_ts = cached
        if now < expire_ts:
            return ctx

    persona = await find_persona(persona_id)

    if persona:
        chain_narrative = await _load_chain_narrative(persona_id)
        ctx = PersonaContext(
            persona_id=persona_id,
            display_name=persona.display_name,
            persona_lite=(
                chain_narrative
                if chain_narrative is not None
                else persona.persona_lite or ""
            ),
            persona_core=persona.persona_core or "",
            appearance_detail=persona.appearance_detail or "",
            error_messages=persona.error_messages or {},
        )
    else:
        ctx = PersonaContext(
            persona_id=persona_id,
            display_name=persona_id,
            persona_lite="",
        )

    _persona_cache[persona_id] = (ctx, now + _CACHE_TTL)
    return ctx
