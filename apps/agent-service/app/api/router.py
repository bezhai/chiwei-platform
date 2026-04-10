"""
API路由汇总
"""

import os
from datetime import date

from fastapi import APIRouter
from pydantic import BaseModel

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


@api_router.post("/admin/debug-glimpse", tags=["Admin"])
async def debug_glimpse(persona_id: str):
    """Glimpse 管线调试端点 — 返回每一步的详细数据，不执行 LLM/写入"""
    from app.orm.memory_crud import get_last_bot_reply_time, get_latest_glimpse_state
    from app.services.glimpse import (
        TARGET_CHAT_ID,
        _is_quiet,
        _now_cst,
        get_unseen_messages,
    )

    now = _now_cst()
    chat_id = TARGET_CHAT_ID
    state = await get_latest_glimpse_state(persona_id, chat_id)
    last_seen = state.last_seen_msg_time if state else 0
    last_obs = (state.observation if state else "")[:100]
    bot_reply_time = await get_last_bot_reply_time(chat_id)
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


@api_router.post("/admin/trigger-voice", tags=["Admin"])
async def trigger_voice(persona_id: str):
    """手动触发一次统一 voice 生成（内心独白 + 风格示例）"""
    from app.services.voice_generator import generate_voice
    result = await generate_voice(persona_id, source="manual")
    return {"ok": True, "persona_id": persona_id, "result": result[:200] if result else None}


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


class RebuildRelationshipMemoryRequest(BaseModel):
    persona_ids: list[str]
    chat_ids: list[str]
    start_time: str  # ISO 8601
    end_time: str  # ISO 8601


@api_router.post("/admin/rebuild-relationship-memory", tags=["Admin"])
async def rebuild_relationship_memory(req: RebuildRelationshipMemoryRequest):
    """批量回溯重建关系记忆（异步，按天拆分）

    接收时间段，内部按天拆分处理。立即返回，后台逐天执行。
    每天处理完打日志，通过 make logs KEYWORD=rebuild 追踪进度。
    """
    import asyncio
    import logging

    logger = logging.getLogger(__name__)

    async def _run():
        from datetime import datetime, timedelta
        from app.orm.crud import get_bot_persona, get_chat_messages_in_range
        from app.services.relationship_memory import (
            extract_relationship_updates,
            format_timeline,
        )

        start_dt = datetime.fromisoformat(req.start_time)
        end_dt = datetime.fromisoformat(req.end_time)

        personas = {}
        for persona_id in req.persona_ids:
            persona = await get_bot_persona(persona_id)
            if persona:
                personas[persona_id] = persona.display_name or persona_id

        total_days = (end_dt.date() - start_dt.date()).days
        logger.info(
            f"[rebuild] Starting: {len(personas)} personas, {len(req.chat_ids)} chats, "
            f"{total_days} days ({start_dt.date()} ~ {end_dt.date()})"
        )

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
                messages = await get_chat_messages_in_range(
                    chat_id, day_start_ts, day_end_ts, limit=5000
                )
                if not messages:
                    continue

                messages.sort(key=lambda m: m.create_time)

                user_ids = list({
                    m.user_id for m in messages
                    if m.role == "user" and m.user_id and m.user_id != "__proactive__"
                })
                if not user_ids:
                    continue

                for persona_id, persona_name in personas.items():
                    try:
                        timeline = await format_timeline(messages, persona_name)
                        if not timeline:
                            continue
                        await extract_relationship_updates(
                            persona_id=persona_id,
                            chat_id=chat_id,
                            user_ids=user_ids,
                            messages_timeline=timeline,
                        )
                        logger.info(
                            f"[rebuild] {day_str} {persona_id}: "
                            f"{len(user_ids)} users, {len(messages)} msgs"
                        )
                    except Exception as e:
                        logger.error(f"[rebuild] {day_str} {persona_id} failed: {e}")

            logger.info(f"[rebuild] Day {day_count}/{total_days} ({day_str}) done.")
            day = next_day

        logger.info("[rebuild] All done.")

    asyncio.create_task(_run())
    return {
        "status": "started",
        "message": f"Rebuild started in background. "
        f"{len(req.persona_ids)} personas, {len(req.chat_ids)} chats, "
        f"check logs with: make logs KEYWORD=rebuild LANE=rel-mem",
    }

