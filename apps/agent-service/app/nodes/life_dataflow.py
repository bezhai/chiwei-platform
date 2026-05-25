"""Phase 4 dataflow nodes: fan-out + business per-persona nodes.

业务逻辑零搬迁 —— 每个 business node 都套薄壳调原函数（life.engine.tick /
memory.voice.generate_voice / reviewer.run_*_for_persona / schedule.
_run_persona_pipeline）。本期是调度层迁移；后续 phase 在重写 chat 时
再回头看薄壳要不要去掉。

glimpse 相关节点（Task 6）在本文件之外，单独处理。
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from app.data.queries import list_all_persona_ids
from app.domain.life_dataflow import (
    DailyPlanRequest,
    DailyPlanTick,
    GlimpseRequest,
    GlimpseTick,
    GlimpseTickRequest,
    HeavyReviewRequest,
    HeavyReviewTick,
    LifeStateChanged,
    LifeTickRequest,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
    MinuteTick,
    SharedDailyContext,
    VoiceRequest,
)
from app.infra.config import settings
from app.runtime import emit, node

logger = logging.getLogger(__name__)
CST = ZoneInfo("Asia/Shanghai")

_LIFE_TICK_TIMEOUT_S = 120.0
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
async def fan_out_life_tick(t: MinuteTick) -> LifeTickRequest | None:
    if not _is_prod():
        return
    return LifeTickRequest(ts=t.ts)


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
# Daily plan：shared 节点先跑一次 → SharedDailyContext → fan-out per-persona
# ---------------------------------------------------------------------------


@node
async def run_shared_daily_pipeline_node(t: DailyPlanTick) -> SharedDailyContext | None:
    if not _is_prod():
        return None
    from app.life.schedule import _run_shared_pipeline
    target_date = datetime.now(CST).date()
    wild, anchors, theater = await _run_shared_pipeline(target_date)
    return SharedDailyContext(
        target_date=target_date.isoformat(),
        wild_materials=wild,
        search_anchors=anchors or "",
        theater=theater,
    )


@node
async def fan_out_daily_plan(c: SharedDailyContext) -> DailyPlanRequest:
    return DailyPlanRequest(
        target_date=c.target_date,
        wild_materials=c.wild_materials,
        search_anchors=c.search_anchors,
        theater=c.theater,
    )


# ---------------------------------------------------------------------------
# Per-persona business @node — 薄壳调原函数；本期不动业务实现
# ---------------------------------------------------------------------------


@node
async def life_tick_node(r: LifeTickRequest) -> None:
    from app.life.engine import tick
    try:
        await asyncio.wait_for(
            tick(r.persona_id),
            timeout=_LIFE_TICK_TIMEOUT_S,
        )
    except TimeoutError:
        logger.error(
            "[%s] life_tick timed out after %.0fs",
            r.persona_id,
            _LIFE_TICK_TIMEOUT_S,
        )
    except Exception:
        logger.exception("[%s] life_tick failed", r.persona_id)


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


@node
async def daily_plan_node(r: DailyPlanRequest) -> None:
    from datetime import date as _date

    from app.life.schedule import _run_persona_pipeline
    try:
        await _run_persona_pipeline(
            r.persona_id,
            _date.fromisoformat(r.target_date),
            r.wild_materials,
            r.search_anchors,
            r.theater,
        )
    except Exception:
        logger.exception("[%s] daily_plan failed", r.persona_id)


# ---------------------------------------------------------------------------
# Glimpse 双路径：5min 周期 + 切到 browsing 即时事件，汇入 GlimpseRequest
# ---------------------------------------------------------------------------


def _new_glimpse_request(persona_id: str, chat_id: str, ts: str, kind: str) -> GlimpseRequest:
    """统一构造 GlimpseRequest —— request_id 是 emit 端生成的 uuid4，
    durable consumer 在 redelivery 时复用同一 id 让 ``insert_idempotent`` 拒重."""
    return GlimpseRequest(
        request_id=str(uuid.uuid4()),
        persona_id=persona_id,
        chat_id=chat_id,
        ts=ts,
        trigger_kind=kind,
    )


@node
async def fan_out_glimpse(t: GlimpseTick) -> GlimpseTickRequest | None:
    """5min cron → emit GlimpseTickRequest template; the wire's
    ``.fan_out_per(_persona_dicts)`` fans it into per-persona copies."""
    if not _is_prod():
        return
    return GlimpseTickRequest(ts=t.ts)


@node
async def glimpse_tick_node(r: GlimpseTickRequest) -> None:
    """5min 周期路径：读 life_state 判 activity，决定要不要 emit GlimpseRequest.

    业务语义跟现状 cron_glimpse 完全一致：sleeping 跳过；browsing 必发；
    其他活动 15% 概率发。读 pg 失败按"这拍跳过"处理，下一拍恢复。
    """
    from app.data.queries import find_latest_life_state
    from app.life.glimpse import list_target_groups
    try:
        state = await find_latest_life_state(r.persona_id)
    except Exception:
        logger.exception("[%s] glimpse_tick read life_state failed", r.persona_id)
        return
    activity = state.activity_type if state else ""
    if activity == "sleeping":
        return
    if activity != "browsing" and random.random() >= 0.15:
        return
    for chat_id in list_target_groups():
        try:
            await emit(_new_glimpse_request(r.persona_id, chat_id, r.ts, "tick"))
        except Exception:
            logger.exception("[%s][%s] glimpse_tick emit failed", r.persona_id, chat_id)


@node
async def glimpse_event_node(c: LifeStateChanged) -> None:
    """即时路径：仅在切到 browsing 瞬间补一拍 GlimpseRequest.

    其他状态切换（如切到 working / sleeping）不在事件路径触发 ——
    "持续期反复刷"由 5min cron 路径承担。
    """
    if not _is_prod():
        return
    if c.activity_type != "browsing":
        return
    if c.activity_type == c.prev_activity_type:
        return  # 段内 refresh 不响应
    from app.life.glimpse import list_target_groups
    for chat_id in list_target_groups():
        try:
            await emit(_new_glimpse_request(c.persona_id, chat_id, c.ts, "event"))
        except Exception:
            logger.exception("[%s][%s] glimpse_event emit failed", c.persona_id, chat_id)


@node
async def run_glimpse_node(r: GlimpseRequest) -> None:
    """LLM 重活，走 .durable() consumer。两条上游路径汇入这里."""
    from app.life.glimpse import run_glimpse
    await run_glimpse(r.persona_id, r.chat_id)
