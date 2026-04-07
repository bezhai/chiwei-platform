"""
API路由汇总
"""

import os
from datetime import date

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


@api_router.post("/admin/trigger-life-engine-tick", tags=["Admin"])
async def trigger_life_engine_tick(
    persona_id: str,
    dry_run: bool = True,
):
    """手动触发一次 Life Engine tick

    Args:
        persona_id: 角色 ID
        dry_run: True 则不写 DB，只返回 LLM 决策结果
    """
    from app.services.life_engine import LifeEngine

    engine = LifeEngine()
    result = await engine.tick(persona_id, dry_run=dry_run)
    return {"ok": True, "persona_id": persona_id, "dry_run": dry_run, "result": result}


@api_router.post("/admin/trigger-glimpse", tags=["Admin"])
async def trigger_glimpse(persona_id: str):
    """手动触发一次 Glimpse 窥屏观察

    不检查 browsing 状态和泳道限制，强制执行。
    """
    from app.services.glimpse import run_glimpse

    result = await run_glimpse(persona_id)
    return {"ok": True, "persona_id": persona_id, "result": result}


@api_router.post("/admin/trigger-schedule", tags=["Admin"])
async def trigger_schedule(
    persona_id: str,
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
        content = await generate_monthly_plan(persona_id=persona_id, target_date=d)
        return {"ok": bool(content), "plan_type": "monthly", "content": content}
    elif plan_type == "weekly":
        content = await generate_weekly_plan(persona_id=persona_id, target_date=d)
        return {"ok": bool(content), "plan_type": "weekly", "content": content}
    elif plan_type == "daily":
        content = await generate_daily_plan(persona_id=persona_id, target_date=d)
        return {"ok": bool(content), "plan_type": "daily", "content": content}
    else:
        return {"ok": False, "message": f"Unknown plan_type: {plan_type}"}


