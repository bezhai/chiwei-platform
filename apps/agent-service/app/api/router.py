"""
API路由汇总
"""

import asyncio
import logging
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


@api_router.post("/admin/trigger-weekly-review", tags=["Admin"])
async def trigger_weekly_review(chat_id: str, persona_id: str, week_start: str | None = None):
    """手动触发周记生成

    Args:
        chat_id: 群 ID
        week_start: 目标周的周一日期（如 "2026-03-03"），默认上周一
    """
    from app.workers.diary_worker import generate_weekly_review_for_chat

    target_monday = date.fromisoformat(week_start) if week_start else None
    content = await generate_weekly_review_for_chat(chat_id, persona_id=persona_id, target_monday=target_monday)
    if content is None:
        return {"ok": False, "message": "该周无日记，跳过"}
    return {"ok": True, "content": content}


@api_router.post("/admin/trigger-journal", tags=["Admin"])
async def trigger_journal(
    persona_id: str,
    journal_type: str = "daily",
    target_date: str | None = None,
    backfill_start: str | None = None,
    backfill_end: str | None = None,
):
    """手动触发 Journal 生成

    单日模式：指定 target_date
    批量回溯：指定 backfill_start + backfill_end

    Args:
        persona_id: persona 标识
        journal_type: "daily" | "weekly"
        target_date: 单日模式的目标日期
        backfill_start: 批量回溯起始日期
        backfill_end: 批量回溯结束日期
    """
    from app.workers.journal_worker import generate_daily_journal, generate_weekly_journal

    # 批量回溯模式
    if backfill_start and backfill_end:
        start = date.fromisoformat(backfill_start)
        end = date.fromisoformat(backfill_end)
        results = []
        current = start
        while current <= end:
            try:
                if journal_type == "daily":
                    content = await generate_daily_journal(current, persona_id=persona_id)
                else:
                    content = await generate_weekly_journal(current, persona_id=persona_id)
                results.append({
                    "date": current.isoformat(),
                    "ok": bool(content),
                    "chars": len(content) if content else 0,
                })
            except Exception as e:
                results.append({"date": current.isoformat(), "ok": False, "error": str(e)})
            current += timedelta(days=1 if journal_type == "daily" else 7)
        return {"ok": True, "journal_type": journal_type, "results": results}

    # 单日模式
    d = date.fromisoformat(target_date) if target_date else date.today() - timedelta(days=1)
    if journal_type == "daily":
        content = await generate_daily_journal(d, persona_id=persona_id)
    elif journal_type == "weekly":
        content = await generate_weekly_journal(d, persona_id=persona_id)
    else:
        return {"ok": False, "message": f"Unknown journal_type: {journal_type}"}

    return {"ok": bool(content), "journal_type": journal_type, "date": d.isoformat(), "content": content}


@api_router.post("/admin/trigger-diary", tags=["Admin"])
async def trigger_diary(chat_id: str, persona_id: str, target_date: str | None = None):
    """手动触发日记生成

    Args:
        chat_id: 群 ID
        persona_id: persona 标识
        target_date: 目标日期（如 "2026-03-12"），默认昨天
    """
    from app.workers.diary_worker import generate_diary_for_chat

    d = date.fromisoformat(target_date) if target_date else date.today() - timedelta(days=1)
    content = await generate_diary_for_chat(chat_id, d, persona_id=persona_id)
    if content is None:
        return {"ok": False, "message": "该日无消息，跳过"}
    return {"ok": True, "date": d.isoformat(), "content": content}


@api_router.post("/admin/trigger-nightly", tags=["Admin"])
async def trigger_nightly(target_date: str | None = None):
    """手动触发完整夜间管线（后台运行）: diary → journal → schedule

    Args:
        target_date: diary/journal 源日期（默认昨天），schedule 自动 +1 天
    """
    from app.orm.crud import get_all_persona_ids

    logger = logging.getLogger(__name__)
    diary_date = date.fromisoformat(target_date) if target_date else date.today() - timedelta(days=1)
    schedule_date = diary_date + timedelta(days=1)
    persona_ids = await get_all_persona_ids()

    async def _pipeline():
        from app.orm.crud import get_active_diary_chat_ids, get_active_p2p_chat_ids
        from app.workers.diary_worker import generate_diary_for_chat
        from app.workers.journal_worker import generate_daily_journal
        from app.workers.schedule_worker import generate_daily_plan

        all_chat_ids = (
            await get_active_diary_chat_ids(min_replies=5, days=7)
            + await get_active_p2p_chat_ids(min_replies=2, days=1)
        )
        for persona_id in persona_ids:
            for chat_id in all_chat_ids:
                try:
                    await generate_diary_for_chat(chat_id, diary_date, persona_id=persona_id)
                except Exception as e:
                    logger.error(f"[nightly][{persona_id}] Diary failed for {chat_id}: {e}")
        for persona_id in persona_ids:
            try:
                await generate_daily_journal(diary_date, persona_id=persona_id)
            except Exception as e:
                logger.error(f"[nightly][{persona_id}] Journal failed: {e}")
        for persona_id in persona_ids:
            try:
                await generate_daily_plan(persona_id=persona_id, target_date=schedule_date)
            except Exception as e:
                logger.error(f"[nightly][{persona_id}] Schedule failed: {e}")
        logger.info(f"[nightly] Pipeline done: diary/journal={diary_date}, schedule={schedule_date}")

    asyncio.create_task(_pipeline())
    return {
        "ok": True,
        "message": f"Pipeline started: diary/journal={diary_date}, schedule={schedule_date}",
        "personas": persona_ids,
    }
