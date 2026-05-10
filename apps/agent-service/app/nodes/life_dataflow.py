"""Phase 4 dataflow nodes: fan-out + business per-persona nodes.

业务逻辑零搬迁 —— 每个 business node 都套薄壳调原函数（life.engine.tick /
memory.voice.generate_voice / reviewer.run_*_for_persona / schedule.
_run_persona_pipeline）。本期是调度层迁移；后续 phase 在重写 chat 时
再回头看薄壳要不要去掉。

glimpse 相关节点（Task 6）在本文件之外，单独处理。
"""
from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_prod() -> bool:
    """Lane gate — fan-out 在非 prod 直接 return，不 emit per-persona request."""
    return not (settings.lane and settings.lane != "prod")


async def _list_persona_ids() -> list[str]:
    return await list_all_persona_ids()


async def _fan_out_per_persona(label: str, build_request) -> None:
    """通用 fan-out：包住 list_persona_ids + emit 循环，所有异常 log 不冒泡.

    DB 抖动 / emit 失败一律不扔回 source loop —— 否则 ``_record_source_error``
    会让进程退出。fan-out 失败的代价是这一拍丢，下一拍自然恢复。
    """
    try:
        pids = await _list_persona_ids()
    except Exception:
        logger.exception("%s: list_persona_ids failed", label)
        return
    for pid in pids:
        try:
            await emit(build_request(pid))
        except Exception:
            logger.exception("[%s] %s fan-out failed", pid, label)


# ---------------------------------------------------------------------------
# Cron fan-out @node
# ---------------------------------------------------------------------------


@node
async def fan_out_life_tick(t: MinuteTick) -> None:
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "life_tick", lambda pid: LifeTickRequest(persona_id=pid, ts=t.ts)
    )


@node
async def fan_out_voice(t: MinuteTick) -> None:
    if not _is_prod():
        return
    cst_ts = datetime.fromisoformat(t.ts).astimezone(CST)
    if cst_ts.hour not in range(8, 24):
        return
    if cst_ts.minute != 0:
        return  # voice 整点触发
    await _fan_out_per_persona(
        "voice", lambda pid: VoiceRequest(persona_id=pid, ts=t.ts)
    )


@node
async def fan_out_light_day(t: LightDayTick) -> None:
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "light_day",
        lambda pid: LightReviewRequest(persona_id=pid, ts=t.ts, window_minutes=30),
    )


@node
async def fan_out_light_night(t: LightNightTick) -> None:
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "light_night",
        lambda pid: LightReviewRequest(persona_id=pid, ts=t.ts, window_minutes=60),
    )


@node
async def fan_out_heavy(t: HeavyReviewTick) -> None:
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "heavy", lambda pid: HeavyReviewRequest(persona_id=pid, ts=t.ts)
    )


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
async def fan_out_daily_plan(c: SharedDailyContext) -> None:
    await _fan_out_per_persona(
        "daily_plan",
        lambda pid: DailyPlanRequest(
            persona_id=pid,
            target_date=c.target_date,
            wild_materials=c.wild_materials,
            search_anchors=c.search_anchors,
            theater=c.theater,
        ),
    )


# ---------------------------------------------------------------------------
# Per-persona business @node — 薄壳调原函数；本期不动业务实现
# ---------------------------------------------------------------------------


@node
async def life_tick_node(r: LifeTickRequest) -> None:
    from app.life.engine import tick
    try:
        await tick(r.persona_id)
    except Exception:
        logger.exception("[%s] life_tick failed", r.persona_id)


@node
async def voice_node(r: VoiceRequest) -> None:
    from app.memory.voice import generate_voice
    try:
        await generate_voice(r.persona_id)
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
async def fan_out_glimpse(t: GlimpseTick) -> None:
    """5min cron → 对每个 persona emit GlimpseTickRequest."""
    if not _is_prod():
        return
    await _fan_out_per_persona(
        "glimpse_tick", lambda pid: GlimpseTickRequest(persona_id=pid, ts=t.ts)
    )


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
