"""赤尾日程 CRUD API"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.orm.crud import (
    delete_schedule,
    get_daily_entries_for_date,
    list_schedules,
    upsert_schedule,
)
from app.orm.models import AkaoSchedule
from app.services.schedule_context import build_schedule_context

router = APIRouter(prefix="/api/schedule", tags=["Schedule"])


class ScheduleCreate(BaseModel):
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


@router.get("")
async def api_list_schedules(
    plan_type: str | None = None,
    active_only: bool = True,
    limit: int = 50,
):
    entries = await list_schedules(plan_type=plan_type, active_only=active_only, limit=limit)
    return [_to_out(e) for e in entries]


@router.get("/current")
async def api_current_schedule():
    """返回当前时刻的日程上下文（和注入 prompt 的内容一致）"""
    context = await build_schedule_context()
    return {"context": context}


@router.get("/daily/{target_date}")
async def api_daily_entries(target_date: str):
    """查指定日期的所有日计划时段"""
    entries = await get_daily_entries_for_date(target_date)
    return [_to_out(e) for e in entries]


@router.post("")
async def api_create_schedule(body: ScheduleCreate):
    if body.plan_type not in ("monthly", "weekly", "daily"):
        raise HTTPException(400, "plan_type must be monthly, weekly, or daily")
    if body.plan_type == "daily" and (not body.time_start or not body.time_end):
        raise HTTPException(400, "daily entries require time_start and time_end")

    entry = AkaoSchedule(
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
    saved = await upsert_schedule(entry)
    return _to_out(saved)


@router.delete("/{schedule_id}")
async def api_delete_schedule(schedule_id: int):
    ok = await delete_schedule(schedule_id)
    if not ok:
        raise HTTPException(404, "Schedule entry not found")
    return {"ok": True}
