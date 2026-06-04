"""Cron-tick dataflow nodes: voice + light/heavy reviewer fan-out.

Each business node is a thin shell over the underlying function
(memory.voice.generate_voice / reviewer.run_*_for_persona). Lane gate +
time filters live in the fan-out @node because they don't depend on
persona identity; the wire's ``.fan_out_per(_persona_dicts)`` expands the
template Request into per-persona copies with failure isolation.

旧 life tick / glimpse / daily-plan 节点已在 world/life 重写中删除——它们的活
由 world engine + life_wake_node 接管。voice + light/heavy reviewer 的 cron
保留。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from app.data.queries import list_all_persona_ids
from app.domain.life_dataflow import (
    HeavyReviewRequest,
    HeavyReviewTick,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
    MinuteTick,
    VoiceRequest,
)
from app.infra.config import settings
from app.runtime import node

logger = logging.getLogger(__name__)
CST = ZoneInfo("Asia/Shanghai")

_VOICE_TIMEOUT_S = 180.0


# ---------------------------------------------------------------------------
# Helpers — used by ``.fan_out_per(_persona_dicts)`` on the wires below.
# ---------------------------------------------------------------------------


def _is_prod() -> bool:
    """Lane gate — fan-out 在非 prod 直接 return，不 emit per-persona request."""
    return not (settings.lane and settings.lane != "prod")


async def _persona_dicts() -> list[dict]:
    """Wire-level fan_out_per extractor.

    Returns the list of ``{"persona_id": pid}`` dicts the runtime merges
    into the in-flight template Data via ``model_copy(update=...)``.
    Failure here (DB jitter on persona listing) is swallowed and logged
    by ``emit._dispatch_fan_out`` so the source loop never sees the
    exception — same fail-soft guarantee the old hand-rolled
    ``_fan_out_per_persona`` provided around ``list_all_persona_ids``.
    """
    pids = await list_all_persona_ids()
    return [{"persona_id": pid} for pid in pids]


# ---------------------------------------------------------------------------
# Cron tick @node — emit a per-persona template Request; the wire's
# ``.fan_out_per(_persona_dicts)`` then fans it into per-key copies with
# failure isolation between personas. Lane gate and time filters stay
# here because they don't depend on persona identity.
# ---------------------------------------------------------------------------


@node
async def fan_out_voice(t: MinuteTick) -> VoiceRequest | None:
    if not _is_prod():
        return
    cst_ts = datetime.fromisoformat(t.ts).astimezone(CST)
    if cst_ts.hour not in range(8, 24):
        return
    if cst_ts.minute != 0:
        return  # voice 整点触发
    return VoiceRequest(ts=t.ts)


@node
async def fan_out_light_day(t: LightDayTick) -> LightReviewRequest | None:
    if not _is_prod():
        return
    return LightReviewRequest(ts=t.ts, window_minutes=30)


@node
async def fan_out_light_night(t: LightNightTick) -> LightReviewRequest | None:
    if not _is_prod():
        return
    return LightReviewRequest(ts=t.ts, window_minutes=60)


@node
async def fan_out_heavy(t: HeavyReviewTick) -> HeavyReviewRequest | None:
    if not _is_prod():
        return
    return HeavyReviewRequest(ts=t.ts)


# ---------------------------------------------------------------------------
# Per-persona business @node — 薄壳调原函数
# ---------------------------------------------------------------------------


@node
async def voice_node(r: VoiceRequest) -> None:
    from app.memory.voice import generate_voice
    try:
        await asyncio.wait_for(
            generate_voice(r.persona_id),
            timeout=_VOICE_TIMEOUT_S,
        )
    except TimeoutError:
        logger.error(
            "[%s] voice timed out after %.0fs",
            r.persona_id,
            _VOICE_TIMEOUT_S,
        )
    except Exception:
        logger.exception("[%s] voice failed", r.persona_id)


@node
async def light_review_node(r: LightReviewRequest) -> None:
    from app.memory.reviewer.light import run_light_review
    try:
        await run_light_review(persona_id=r.persona_id, window_minutes=r.window_minutes)
    except Exception:
        logger.exception("[%s] light_review failed", r.persona_id)


@node
async def heavy_review_node(r: HeavyReviewRequest) -> None:
    from app.memory.reviewer.heavy import run_heavy_review_for_persona
    try:
        await run_heavy_review_for_persona(r.persona_id)
    except Exception:
        logger.exception("[%s] heavy_review failed", r.persona_id)
