"""API routes — health, admin triggers, schedule CRUD.

Merges old ``api/router.py`` + ``api/schedule.py`` into one module.
All routes use new module imports (``app.data.*``, ``app.life.*``, ``app.memory.*``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.data.models import AkaoSchedule
from app.data.queries import (
    delete_schedule,
    find_persona,
    list_schedules,
    upsert_schedule,
)
from app.data.session import get_session
from app.life.schedule import build_schedule_context

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/")
async def root():
    return {"message": "FastAPI is running!"}


@router.get("/health", tags=["Health"])
async def health_check():
    return {
        "status": "ok",
        "timestamp": datetime.now().isoformat(),
        "service": "agent-service",
        "version": os.environ.get("GIT_SHA", "unknown"),
    }


# ---------------------------------------------------------------------------
# Admin triggers
# ---------------------------------------------------------------------------


@router.post("/admin/trigger-life-engine-tick", tags=["Admin"])
async def trigger_life_engine_tick(persona_id: str, dry_run: bool = True):
    """Manual Life Engine tick."""
    from app.life.engine import tick

    result = await tick(persona_id, dry_run=dry_run)
    return {"ok": True, "persona_id": persona_id, "dry_run": dry_run, "result": result}


@router.post("/admin/trigger-glimpse", tags=["Admin"])
async def trigger_glimpse(persona_id: str):
    """Manual Glimpse (ignores browsing state + lane restriction)."""
    from app.life.glimpse import run_glimpse

    result = await run_glimpse(persona_id)
    return {"ok": True, "persona_id": persona_id, "result": result}


@router.post("/admin/debug-glimpse", tags=["Admin"])
async def debug_glimpse(persona_id: str):
    """Glimpse debug — return pipeline state without executing LLM."""
    from app.data import queries as Q
    from app.life.glimpse import TARGET_CHAT_ID, _is_quiet, _now_cst
    from app.life.proactive import get_unseen_messages

    now = _now_cst()
    chat_id = TARGET_CHAT_ID

    async with get_session() as s:
        state = await Q.find_latest_glimpse_state(s, persona_id, chat_id)
    last_seen = state.last_seen_msg_time if state else 0
    last_obs = (state.observation if state else "")[:100]

    async with get_session() as s:
        bot_reply_time = await Q.find_last_bot_reply_time(s, chat_id)
    effective_after = max(last_seen, bot_reply_time)
    messages = await get_unseen_messages(chat_id, after=effective_after)

    return {
        "now_cst": now.isoformat(),
        "is_quiet": _is_quiet(now),
        "chat_id": chat_id,
        "last_seen_msg_time": last_seen,
        "last_observation": last_obs,
        "bot_reply_time": bot_reply_time,
        "effective_after": effective_after,
        "unseen_message_count": len(messages),
        "first_msg_time": messages[0].create_time if messages else None,
        "last_msg_time": messages[-1].create_time if messages else None,
    }


@router.post("/admin/trigger-voice", tags=["Admin"])
async def trigger_voice(persona_id: str):
    """Manual voice generation (inner monologue + reply style)."""
    from app.memory.voice import generate_voice

    result = await generate_voice(persona_id, source="manual")
    return {
        "ok": True,
        "persona_id": persona_id,
        "result": result[:200] if result else None,
    }


@router.post("/admin/trigger-schedule", tags=["Admin"])
async def trigger_schedule(
    persona_id: str,
    plan_type: str = "daily",
    target_date: str | None = None,
):
    """Manual schedule generation (monthly / weekly / daily)."""
    from app.life.schedule import (
        generate_daily_plan,
        generate_monthly_plan,
        generate_weekly_plan,
    )

    d = date.fromisoformat(target_date) if target_date else None

    generators = {
        "monthly": generate_monthly_plan,
        "weekly": generate_weekly_plan,
        "daily": generate_daily_plan,
    }
    gen = generators.get(plan_type)
    if not gen:
        return {"ok": False, "message": f"Unknown plan_type: {plan_type}"}

    content = await gen(persona_id=persona_id, target_date=d)
    return {"ok": bool(content), "plan_type": plan_type, "content": content}


# ---------------------------------------------------------------------------
# Rebuild relationship memory (async background task)
# ---------------------------------------------------------------------------


class RebuildRelationshipMemoryRequest(BaseModel):
    persona_ids: list[str]
    chat_ids: list[str]
    start_time: str  # ISO 8601
    end_time: str  # ISO 8601


@router.post("/admin/rebuild-relationship-memory", tags=["Admin"])
async def rebuild_relationship_memory(req: RebuildRelationshipMemoryRequest):
    """Batch rebuild relationship memory (async, day-by-day).

    Returns immediately; track progress via ``make logs KEYWORD=rebuild``.
    """

    async def _run():
        from app.memory.relationships import extract_relationship_updates

        start_dt = datetime.fromisoformat(req.start_time)
        end_dt = datetime.fromisoformat(req.end_time)

        personas: dict[str, str] = {}
        for persona_id in req.persona_ids:
            async with get_session() as s:
                persona = await find_persona(s, persona_id)
            if persona:
                personas[persona_id] = persona.display_name or persona_id

        total_days = (end_dt.date() - start_dt.date()).days
        logger.info(
            "[rebuild] Starting: %d personas, %d chats, %d days (%s ~ %s)",
            len(personas),
            len(req.chat_ids),
            total_days,
            start_dt.date(),
            end_dt.date(),
        )

        from app.data.queries import find_messages_in_range

        day = start_dt
        day_count = 0
        while day < end_dt:
            next_day = day + timedelta(days=1)
            if next_day > end_dt:
                next_day = end_dt
            day_start_ts = int(day.timestamp() * 1000)
            day_end_ts = int(next_day.timestamp() * 1000)
            day_count += 1
            day_str = day.strftime("%m/%d")

            for chat_id in req.chat_ids:
                async with get_session() as s:
                    messages = await find_messages_in_range(
                        s, chat_id, day_start_ts, day_end_ts, limit=5000
                    )
                if not messages:
                    continue

                messages.sort(key=lambda m: m.create_time)
                user_ids = list(
                    {
                        m.user_id
                        for m in messages
                        if m.role == "user"
                        and m.user_id
                        and m.user_id != "__proactive__"
                    }
                )
                if not user_ids:
                    continue

                for persona_id in personas:
                    try:
                        await extract_relationship_updates(
                            persona_id=persona_id,
                            chat_id=chat_id,
                            user_ids=user_ids,
                            messages=messages,
                            source="rebuild",
                        )
                        logger.info(
                            "[rebuild] %s %s: %d users, %d msgs",
                            day_str,
                            persona_id,
                            len(user_ids),
                            len(messages),
                        )
                    except Exception as e:
                        logger.error(
                            "[rebuild] %s %s failed: %s", day_str, persona_id, e
                        )

            logger.info(
                "[rebuild] Day %d/%d (%s) done.", day_count, total_days, day_str
            )
            day = next_day

        logger.info("[rebuild] All done.")

    asyncio.create_task(_run())
    return {
        "status": "started",
        "message": (
            f"Rebuild started in background. "
            f"{len(req.persona_ids)} personas, {len(req.chat_ids)} chats, "
            f"check logs with: make logs KEYWORD=rebuild"
        ),
    }


# ---------------------------------------------------------------------------
# Schedule CRUD
# ---------------------------------------------------------------------------


class ScheduleCreate(BaseModel):
    persona_id: str
    plan_type: str  # "monthly" | "weekly" | "daily"
    period_start: str
    period_end: str
    time_start: str | None = None
    time_end: str | None = None
    content: str
    mood: str | None = None
    energy_level: int | None = None
    response_style_hint: str | None = None
    proactive_action: dict | None = None
    target_chats: list | None = None
    model: str | None = None
    is_active: bool = True


class ScheduleOut(BaseModel):
    id: int
    persona_id: str
    plan_type: str
    period_start: str
    period_end: str
    time_start: str | None
    time_end: str | None
    content: str
    mood: str | None
    energy_level: int | None
    response_style_hint: str | None
    proactive_action: dict | None
    target_chats: list | None
    model: str | None
    is_active: bool

    model_config = {"from_attributes": True}


def _to_out(entry: AkaoSchedule) -> dict:
    return ScheduleOut.model_validate(entry).model_dump()


@router.get("/api/schedule", tags=["Schedule"])
async def api_list_schedules(
    plan_type: str | None = None,
    active_only: bool = True,
    limit: int = 50,
):
    async with get_session() as s:
        entries = await list_schedules(
            s, plan_type=plan_type, active_only=active_only, limit=limit
        )
    return [_to_out(e) for e in entries]


@router.get("/api/schedule/current", tags=["Schedule"])
async def api_current_schedule(persona_id: str):
    """Return the current schedule context (same as injected into prompt)."""
    context = await build_schedule_context(persona_id)
    return {"context": context}


@router.get("/api/schedule/daily/{target_date}", tags=["Schedule"])
async def api_daily_entries(target_date: str, persona_id: str):
    """Return all daily plan entries for a given date and persona."""
    from app.data.queries import find_daily_entries

    async with get_session() as s:
        entries = await find_daily_entries(s, target_date, persona_id)
    return [_to_out(e) for e in entries]


@router.post("/api/schedule", tags=["Schedule"])
async def api_create_schedule(body: ScheduleCreate):
    if body.plan_type not in ("monthly", "weekly", "daily"):
        raise HTTPException(400, "plan_type must be monthly, weekly, or daily")
    if body.plan_type == "daily" and (not body.time_start or not body.time_end):
        raise HTTPException(400, "daily entries require time_start and time_end")

    entry = AkaoSchedule(
        persona_id=body.persona_id,
        plan_type=body.plan_type,
        period_start=body.period_start,
        period_end=body.period_end,
        time_start=body.time_start,
        time_end=body.time_end,
        content=body.content,
        mood=body.mood,
        energy_level=body.energy_level,
        response_style_hint=body.response_style_hint,
        proactive_action=body.proactive_action,
        target_chats=body.target_chats,
        model=body.model,
        is_active=body.is_active,
    )
    async with get_session() as s:
        saved = await upsert_schedule(s, entry)
    return _to_out(saved)


@router.delete("/api/schedule/{schedule_id}", tags=["Schedule"])
async def api_delete_schedule(schedule_id: int):
    async with get_session() as s:
        ok = await delete_schedule(s, schedule_id)
    if not ok:
        raise HTTPException(404, "Schedule entry not found")
    return {"ok": True}
