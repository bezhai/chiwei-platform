"""Life Engine — 赤尾的生活状态机

每分钟 tick，LLM 决定赤尾当前在做什么。
状态持久化在 life_engine_state 表，回复时读取注入 context。
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.orm.base import AsyncSessionLocal
from app.orm.memory_models import LifeEngineState

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))


async def _load_state(persona_id: str) -> LifeEngineState | None:
    """查最新一行状态，不存在返回 None"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LifeEngineState)
            .where(LifeEngineState.persona_id == persona_id)
            .order_by(LifeEngineState.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


async def _save_state(
    persona_id: str,
    current_state: str,
    activity_type: str,
    response_mood: str,
    skip_until: datetime | None,
) -> None:
    """INSERT 一行新状态"""
    async with AsyncSessionLocal() as session:
        row = LifeEngineState(
            persona_id=persona_id,
            current_state=current_state,
            activity_type=activity_type,
            response_mood=response_mood,
            skip_until=skip_until,
        )
        session.add(row)
        await session.commit()


class LifeEngine:
    """赤尾生活状态机"""

    async def tick(self, persona_id: str) -> None:
        """一次心跳：检查 skip → LLM 决策 → 保存 → 副作用"""
        row = await _load_state(persona_id)
        now = datetime.now(CST)

        if row:
            current_state = row.current_state
            response_mood = row.response_mood
            skip_until = row.skip_until
        else:
            current_state = "刚醒来，还有点迷糊"
            response_mood = "迷迷糊糊的"
            skip_until = None

        # skip_until 检查
        if skip_until and now < skip_until:
            return

        # LLM 决策
        new = await self._think(current_state, response_mood, now, persona_id)

        await _save_state(
            persona_id=persona_id,
            current_state=new["current_state"],
            activity_type=new["activity_type"],
            response_mood=new["response_mood"],
            skip_until=new["skip_until"],
        )

        logger.info(
            f"[{persona_id}] tick: {new['activity_type']} "
            f"({new['current_state'][:50]}) "
            f"skip_until={new['skip_until']}"
        )

        # browsing → 触发 glimpse
        if new["activity_type"] == "browsing":
            from app.services.glimpse import run_glimpse

            try:
                await run_glimpse(persona_id)
            except Exception as e:
                logger.error(f"[{persona_id}] Glimpse failed: {e}")

    async def _think(
        self,
        current_state: str,
        response_mood: str,
        now: datetime,
        persona_id: str,
    ) -> dict:
        """调用 LLM 决定下一步状态"""
        from app.agents.infra.langfuse_client import get_prompt
        from app.agents.infra.model_builder import ModelBuilder
        from app.config.config import settings
        from app.orm.crud import get_bot_persona, get_plan_for_period
        from app.orm.memory_crud import get_today_fragments

        persona = await get_bot_persona(persona_id)
        persona_name = persona.display_name if persona else persona_id
        persona_lite = persona.persona_lite if persona else ""

        today = now.strftime("%Y-%m-%d")
        schedule = await get_plan_for_period("daily", today, today, persona_id)
        schedule_text = schedule.content if schedule else "（今天还没有安排）"

        today_frags = await get_today_fragments(
            persona_id, grains=["conversation", "glimpse"]
        )
        frag_text = (
            "\n".join(f.content[:100] for f in today_frags[-5:])
            if today_frags
            else "（今天还没什么经历）"
        )

        prompt = get_prompt("life_engine_tick")
        compiled = prompt.compile(
            persona_name=persona_name,
            persona_lite=persona_lite,
            current_time=now.strftime("%H:%M"),
            current_state=current_state,
            response_mood=response_mood,
            schedule=schedule_text,
            recent_experiences=frag_text,
        )

        model = await ModelBuilder.build_chat_model(settings.life_engine_model)
        response = await model.ainvoke([{"role": "user", "content": compiled}])
        raw = _extract_text(response.content)

        return self._parse_tick_response(raw, current_state, response_mood, now)

    def _parse_tick_response(
        self,
        raw: str,
        fallback_state: str,
        fallback_mood: str,
        now: datetime,
    ) -> dict:
        """解析 LLM tick 响应"""
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(raw[start:end])

                # wake_me_at → skip_until
                skip_until = _parse_wake_me_at(data.get("wake_me_at"), now)

                return {
                    "current_state": data.get("current_state", fallback_state),
                    "activity_type": data.get("activity_type", ""),
                    "response_mood": data.get("response_mood", fallback_mood),
                    "skip_until": skip_until,
                }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse tick response: {e}, raw={raw[:200]}")

        return {
            "current_state": fallback_state,
            "activity_type": "",
            "response_mood": fallback_mood,
            "skip_until": None,
        }


def _parse_wake_me_at(value: str | None, now: datetime) -> datetime | None:
    """解析 wake_me_at HH:MM 为 datetime，null 返回 None"""
    if not value or value == "null":
        return None
    try:
        parts = value.strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
        wake = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # 如果时间已过（比如现在 23:00，wake_me_at 07:00）→ 明天
        if wake <= now:
            wake += timedelta(days=1)
        return wake
    except (ValueError, IndexError):
        logger.warning(f"Invalid wake_me_at: {value}")
        return None


def _extract_text(content) -> str:
    """从 LLM response content 提取纯文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()
