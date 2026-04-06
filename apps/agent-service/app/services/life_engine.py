"""Life Engine — 赤尾的生活状态机

每分钟 tick，LLM 决定赤尾当前在做什么。
状态持久化在 life_engine_state 表，回复时读取注入 context。
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.orm.base import AsyncSessionLocal
from app.orm.memory_models import LifeEngineState

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_VALID_ACTIVITY_TYPES = {"browsing", "sleeping", "out", "busy", "idle"}


async def _load_state(persona_id: str) -> LifeEngineState | None:
    """从 DB 加载状态，不存在返回 None"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(LifeEngineState).where(
                LifeEngineState.persona_id == persona_id
            )
        )
        return result.scalar_one_or_none()


async def _save_state(
    persona_id: str,
    current_state: str,
    activity_type: str,
    response_mood: str,
    skip_until: datetime | None,
) -> None:
    """UPSERT 状态到 DB"""
    async with AsyncSessionLocal() as session:
        stmt = pg_insert(LifeEngineState).values(
            persona_id=persona_id,
            current_state=current_state,
            activity_type=activity_type,
            response_mood=response_mood,
            skip_until=skip_until,
            updated_at=datetime.now(CST),
        ).on_conflict_do_update(
            index_elements=["persona_id"],
            set_={
                "current_state": current_state,
                "activity_type": activity_type,
                "response_mood": response_mood,
                "skip_until": skip_until,
                "updated_at": datetime.now(CST),
            },
        )
        await session.execute(stmt)
        await session.commit()


class LifeEngine:
    """赤尾生活状态机"""

    async def tick(self, persona_id: str) -> None:
        """一次心跳：检查 skip → 加载上下文 → LLM 决策 → 保存 → 副作用"""
        row = await _load_state(persona_id)
        now = datetime.now(CST)

        # 当前状态
        if row:
            current_state = row.current_state
            activity_type = row.activity_type
            response_mood = row.response_mood
            skip_until = row.skip_until
        else:
            current_state = "刚醒来，还有点迷糊"
            activity_type = "idle"
            response_mood = "迷迷糊糊的"
            skip_until = None

        # skip_until 检查
        if skip_until and now < skip_until:
            return

        old_activity = activity_type

        # LLM 决策
        new = await self._think(
            current_state, activity_type, response_mood, now, persona_id
        )

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

        # 状态变化时触发副作用
        if new["activity_type"] != old_activity:
            await self._on_state_change(persona_id, old_activity, new)

    async def _think(
        self,
        current_state: str,
        activity_type: str,
        response_mood: str,
        now: datetime,
        persona_id: str,
    ) -> dict:
        """调用 LLM 决定下一步状态，返回 dict"""
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
            activity_type=activity_type,
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
                skip_minutes = data.get("skip_minutes", 0)
                skip_until = None
                if skip_minutes and int(skip_minutes) > 0:
                    skip_until = now + timedelta(minutes=int(skip_minutes))

                activity = data.get("activity_type", "idle")
                if activity not in _VALID_ACTIVITY_TYPES:
                    activity = "idle"

                return {
                    "current_state": data.get("current_state", fallback_state),
                    "activity_type": activity,
                    "response_mood": data.get("response_mood", fallback_mood),
                    "skip_until": skip_until,
                }
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse tick response: {e}, raw={raw[:200]}")

        return {
            "current_state": fallback_state,
            "activity_type": "idle",
            "response_mood": fallback_mood,
            "skip_until": None,
        }

    async def _on_state_change(
        self, persona_id: str, old_activity: str, new: dict
    ) -> None:
        """状态变化时的副作用"""
        logger.info(
            f"[{persona_id}] State change: {old_activity} → {new['activity_type']} "
            f"({new['current_state'][:40]})"
        )
        if new["activity_type"] == "browsing":
            from app.services.glimpse import run_glimpse

            try:
                await run_glimpse(persona_id)
            except Exception as e:
                logger.error(f"[{persona_id}] Glimpse failed: {e}")


def _extract_text(content) -> str:
    """从 LLM response content 提取纯文本"""
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ).strip()
    return (content or "").strip()
