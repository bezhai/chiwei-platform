"""API routes — health, admin triggers, schedule CRUD.

Merges old ``api/router.py`` + ``api/schedule.py`` into one module.
All routes use new module imports (``app.data.*``, ``app.life.*``, ``app.memory.*``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.data.models import AkaoSchedule
from app.data.queries import (
    delete_schedule,
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
async def trigger_life_engine_tick(persona_id: str, dry_run: bool = True, force: bool = False):
    """Manual Life Engine tick. force=True ignores skip_until and persists."""
    from app.life.engine import tick

    result = await tick(persona_id, dry_run=dry_run, force=force)
    return {"ok": True, "persona_id": persona_id, "dry_run": dry_run, "force": force, "result": result}


@router.post("/admin/trigger-glimpse", tags=["Admin"])
async def trigger_glimpse(persona_id: str):
    """Manual Glimpse (ignores browsing state + lane restriction)."""
    from app.life.glimpse import list_target_groups, run_glimpse

    results = {}
    for chat_id in list_target_groups():
        results[chat_id] = await run_glimpse(persona_id, chat_id)
    return {"ok": True, "persona_id": persona_id, "results": results}


@router.post("/admin/debug-glimpse", tags=["Admin"])
async def debug_glimpse(persona_id: str):
    """Glimpse debug — return pipeline state without executing LLM."""
    from app.data import queries as Q
    from app.life.glimpse import _now_cst, list_target_groups
    from app.life.proactive import get_unseen_messages

    now = _now_cst()
    groups_info = []
    for chat_id in list_target_groups():
        async with get_session() as s:
            state = await Q.find_latest_glimpse_state(s, persona_id, chat_id)
        last_seen = state.last_seen_msg_time if state else 0
        last_obs = (state.observation if state else "")[:100]

        async with get_session() as s:
            bot_reply_time = await Q.find_last_bot_reply_time(s, chat_id)
        effective_after = max(last_seen, bot_reply_time)
        messages = await get_unseen_messages(chat_id, after=effective_after)

        groups_info.append({
            "chat_id": chat_id,
            "last_seen_msg_time": last_seen,
            "last_observation": last_obs,
            "bot_reply_time": bot_reply_time,
            "effective_after": effective_after,
            "unseen_message_count": len(messages),
            "first_msg_time": messages[0].create_time if messages else None,
            "last_msg_time": messages[-1].create_time if messages else None,
        })

    return {
        "now_cst": now.isoformat(),
        "groups": groups_info,
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
    """Manual schedule generation (daily only). Fire-and-forget; check logs/trace."""
    if plan_type != "daily":
        return {"ok": False, "message": f"Only 'daily' plan_type is supported. Got: {plan_type}"}

    from app.life.schedule import generate_daily_plan

    d = date.fromisoformat(target_date) if target_date else None
    asyncio.create_task(generate_daily_plan(persona_id=persona_id, target_date=d))
    return {
        "ok": True,
        "plan_type": plan_type,
        "message": (
            f"Schedule generation started for {persona_id} "
            f"(target={d.isoformat() if d else 'today'}). "
            f"Check `make logs APP=agent-service KEYWORD=daily plan` or Langfuse trace."
        ),
    }


# ---------------------------------------------------------------------------
# Search (experiment helper — wraps the existing search_web tool)
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    queries: list[str]
    num: int = 5


@router.post("/admin/search", tags=["Admin"])
async def admin_search(req: SearchRequest):
    """Batch web search — runs multiple queries, returns raw results."""
    from app.agent.tools.search import _you_search
    from app.infra.config import settings

    if not settings.you_search_host:
        raise HTTPException(503, "You Search API not configured")

    results = {}
    for query in req.queries:
        try:
            hits = await _you_search(query, req.num, "CN", "ZH-HANS")
            results[query] = hits
        except Exception as e:
            results[query] = {"error": str(e)}

    return results


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
    persona_id: str | None = None,
    active_only: bool = True,
    limit: int = 50,
):
    async with get_session() as s:
        entries = await list_schedules(
            s, plan_type=plan_type, persona_id=persona_id, active_only=active_only, limit=limit
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
