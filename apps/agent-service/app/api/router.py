"""
API路由汇总
"""

import os
from datetime import date, timedelta

from fastapi import APIRouter

# 创建主路由
api_router = APIRouter()


# 健康检查路由
@api_router.get("/")
async def root():
    return {"message": "FastAPI is running!"}


# 专用健康检查端点
@api_router.get("/health", tags=["Health"])
async def health_check():
    """
    服务健康检查端点
    """
    # 可在这里添加更多健康检查的逻辑
    return {
        "status": "ok",
        "timestamp": import_time(),
        "service": "agent-service",
        "version": os.environ.get("GIT_SHA", "unknown"),
    }


def import_time():
    """获取当前时间字符串"""
    from datetime import datetime

    return datetime.now().isoformat()


# ==================== 调试端点 ====================


@api_router.post("/admin/trigger-schedule", tags=["Admin"])
async def trigger_schedule(
    plan_type: str = "daily",
    target_date: str | None = None,
):
    """手动触发日程生成

    Args:
        plan_type: "monthly" | "weekly" | "daily"
        target_date: 目标日期（如 "2026-03-18"），默认今天
    """
    from app.workers.schedule_worker import (
        generate_daily_plan,
        generate_monthly_plan,
        generate_weekly_plan,
    )

    d = date.fromisoformat(target_date) if target_date else None
    if plan_type == "monthly":
        content = await generate_monthly_plan(d)
        return {"ok": bool(content), "plan_type": "monthly", "content": content}
    elif plan_type == "weekly":
        content = await generate_weekly_plan(d)
        return {"ok": bool(content), "plan_type": "weekly", "content": content}
    elif plan_type == "daily":
        content = await generate_daily_plan(d)
        return {"ok": bool(content), "plan_type": "daily", "content": content}
    else:
        return {"ok": False, "message": f"Unknown plan_type: {plan_type}"}


@api_router.post("/admin/trigger-weekly-review", tags=["Admin"])
async def trigger_weekly_review(chat_id: str, week_start: str | None = None):
    """手动触发周记生成

    Args:
        chat_id: 群 ID
        week_start: 目标周的周一日期（如 "2026-03-03"），默认上周一
    """
    from app.workers.diary_worker import generate_weekly_review_for_chat

    target_monday = date.fromisoformat(week_start) if week_start else None
    content = await generate_weekly_review_for_chat(chat_id, target_monday)
    if content is None:
        return {"ok": False, "message": "该周无日记，跳过"}
    return {"ok": True, "content": content}


@api_router.post("/admin/trigger-diary", tags=["Admin"])
async def trigger_diary(chat_id: str, target_date: str | None = None):
    """手动触发日记生成

    Args:
        chat_id: 群 ID
        target_date: 目标日期（如 "2026-03-12"），默认昨天
    """
    from app.workers.diary_worker import generate_diary_for_chat

    d = date.fromisoformat(target_date) if target_date else date.today() - timedelta(days=1)
    content = await generate_diary_for_chat(chat_id, d)
    if content is None:
        return {"ok": False, "message": "该日无消息，跳过"}
    return {"ok": True, "date": d.isoformat(), "content": content}
