"""Admin / public API @nodes — Phase 6 v4 Gap 1 business closure.

Each node corresponds to one HTTP endpoint; wires in app/wiring/admin.py.
Bodies preserve old routes.py response shapes verbatim. Return types are
left un-annotated so the @node decorator skips Data-only validation —
these nodes return dict / list[dict] for sync HTTP RPC.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from app.data.models import AkaoSchedule
from app.data.queries import (
    delete_schedule,
    find_daily_entries,
    list_schedules,
    upsert_schedule,
)
from app.data.session import get_session
from app.domain.admin import (
    AdminGlimpseRequest,
    AdminLifeTickRequest,
    AdminScheduleRequest,
    AdminSearchRequest,
    AdminVoiceRequest,
    DebugGlimpseRequest,
    ScheduleCreateRequest,
    ScheduleCurrentRequest,
    ScheduleDailyRequest,
    ScheduleDeleteRequest,
    ScheduleListRequest,
    ScheduleOut,
)
from app.runtime import node


@node
async def admin_life_tick_node(r: AdminLifeTickRequest):
    from app.life.engine import tick

    result = await tick(r.persona_id, dry_run=r.dry_run, force=r.force)
    return {
        "ok": True,
        "persona_id": r.persona_id,
        "dry_run": r.dry_run,
        "force": r.force,
        "result": result,
    }


@node
async def admin_trigger_glimpse_node(r: AdminGlimpseRequest):
    from app.life.glimpse import list_target_groups, run_glimpse

    results: dict[str, Any] = {}
    for chat_id in list_target_groups():
        results[chat_id] = await run_glimpse(r.persona_id, chat_id)
    return {"ok": True, "persona_id": r.persona_id, "results": results}


@node
async def admin_debug_glimpse_node(r: DebugGlimpseRequest):
    from app.data import queries as Q
    from app.life.glimpse import _now_cst, list_target_groups
    from app.life.proactive import get_unseen_messages

    now = _now_cst()
    groups_info = []
    for chat_id in list_target_groups():
        async with get_session() as s:
            state = await Q.find_latest_glimpse_state(s, r.persona_id, chat_id)
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
    return {"now_cst": now.isoformat(), "groups": groups_info}


@node
async def admin_trigger_voice_node(r: AdminVoiceRequest):
    from app.memory.voice import generate_voice

    result = await generate_voice(r.persona_id, source="manual")
    return {
        "ok": True,
        "persona_id": r.persona_id,
        "result": result[:200] if result else None,
    }


@node
async def admin_trigger_schedule_node(r: AdminScheduleRequest):
    if r.plan_type != "daily":
        return {
            "ok": False,
            "message": f"Only 'daily' plan_type is supported. Got: {r.plan_type}",
        }
    from datetime import date

    from app.life.schedule import generate_daily_plan

    d = date.fromisoformat(r.target_date) if r.target_date else None
    # Old code used asyncio.create_task (fire-and-forget). Phase 6 v4 Gap 5
    # removes that hack; we await directly. Endpoint becomes synchronous.
    await generate_daily_plan(persona_id=r.persona_id, target_date=d)
    return {
        "ok": True,
        "plan_type": r.plan_type,
        "message": (
            f"Schedule generation completed for {r.persona_id} "
            f"(target={d.isoformat() if d else 'today'})."
        ),
    }


@node
async def admin_search_node(r: AdminSearchRequest):
    from app.agent.tools.search import _you_search
    from app.infra.config import settings

    if not settings.you_search_host:
        raise HTTPException(503, "You Search API not configured")
    results: dict[str, Any] = {}
    for query in r.queries:
        try:
            hits = await _you_search(query, r.num, "CN", "ZH-HANS")
            results[query] = hits
        except Exception as e:
            results[query] = {"error": str(e)}
    return results


@node
async def list_schedules_node(r: ScheduleListRequest):
    async with get_session() as s:
        entries = await list_schedules(
            s,
            plan_type=r.plan_type,
            persona_id=r.persona_id,
            active_only=r.active_only,
            limit=r.limit,
        )
    return [_to_out(e) for e in entries]


@node
async def current_schedule_node(r: ScheduleCurrentRequest):
    from app.life.schedule import build_schedule_context

    return {"context": await build_schedule_context(r.persona_id)}


@node
async def daily_entries_node(r: ScheduleDailyRequest):
    async with get_session() as s:
        entries = await find_daily_entries(s, r.target_date, r.persona_id)
    return [_to_out(e) for e in entries]


@node
async def create_schedule_node(r: ScheduleCreateRequest):
    if r.plan_type not in ("monthly", "weekly", "daily"):
        raise HTTPException(400, "plan_type must be monthly, weekly, or daily")
    if r.plan_type == "daily" and (not r.time_start or not r.time_end):
        raise HTTPException(400, "daily entries require time_start and time_end")
    entry = AkaoSchedule(
        persona_id=r.persona_id,
        plan_type=r.plan_type,
        period_start=r.period_start,
        period_end=r.period_end,
        time_start=r.time_start,
        time_end=r.time_end,
        content=r.content,
        mood=r.mood,
        energy_level=r.energy_level,
        response_style_hint=r.response_style_hint,
        proactive_action=r.proactive_action,
        target_chats=r.target_chats,
        model=r.model,
        is_active=r.is_active,
    )
    async with get_session() as s:
        saved = await upsert_schedule(s, entry)
    return _to_out(saved)


@node
async def delete_schedule_node(r: ScheduleDeleteRequest):
    async with get_session() as s:
        ok = await delete_schedule(s, r.schedule_id)
    if not ok:
        raise HTTPException(404, "Schedule entry not found")
    return {"ok": True}


def _to_out(entry: AkaoSchedule) -> dict:
    """Serialize via ScheduleOut so response shape auto-syncs with AkaoSchedule."""
    return ScheduleOut.model_validate(entry).model_dump()
