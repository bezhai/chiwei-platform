"""Life Engine — 赤尾的生活状态机

每分钟 tick，LLM 决定赤尾当前在做什么。
状态持久化在 Redis，回复时读取注入 context。
"""

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from app.clients.redis import AsyncRedisClient

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

_REDIS_PREFIX = "life_engine"
_STATE_TTL = 86400  # 24 hours


def _redis_key(persona_id: str) -> str:
    return f"{_REDIS_PREFIX}:{persona_id}"


@dataclass
class LifeState:
    """赤尾的生活状态快照"""

    current_state: str       # "在沙发上刷手机，有点无聊"
    activity_type: str       # browsing | sleeping | out | busy | idle
    response_mood: str       # "心情不错" — 注入 context 影响回复风格
    skip_until: str | None   # ISO8601，None = 每分钟 tick
    updated_at: str          # ISO8601

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "LifeState":
        return cls(
            current_state=data["current_state"],
            activity_type=data["activity_type"],
            response_mood=data["response_mood"],
            skip_until=data.get("skip_until"),
            updated_at=data["updated_at"],
        )

    @classmethod
    def default(cls) -> "LifeState":
        now = datetime.now(CST)
        return cls(
            current_state="刚醒来，还有点迷糊",
            activity_type="idle",
            response_mood="迷迷糊糊的",
            skip_until=None,
            updated_at=now.isoformat(),
        )


class LifeEngine:
    """赤尾生活状态机"""

    async def _load_state(self, persona_id: str) -> LifeState:
        """从 Redis 加载状态，不存在则返回默认"""
        redis = AsyncRedisClient.get_instance()
        raw = await redis.get(_redis_key(persona_id))
        if raw:
            try:
                return LifeState.from_dict(json.loads(raw))
            except (json.JSONDecodeError, KeyError):
                logger.warning(f"[{persona_id}] Corrupt life state, resetting")
        return LifeState.default()

    async def _save_state(self, persona_id: str, state: LifeState) -> None:
        """保存状态到 Redis"""
        redis = AsyncRedisClient.get_instance()
        await redis.set(
            _redis_key(persona_id),
            json.dumps(state.to_dict(), ensure_ascii=False),
            ex=_STATE_TTL,
        )

    async def tick(self, persona_id: str) -> None:
        """一次心跳：检查 skip → 加载上下文 → LLM 决策 → 保存 → 副作用"""
        state = await self._load_state(persona_id)
        now = datetime.now(CST)

        # skip_until 检查
        if state.skip_until:
            try:
                skip_time = datetime.fromisoformat(state.skip_until)
                if now < skip_time:
                    return
            except ValueError:
                pass  # malformed skip_until, proceed

        old_activity = state.activity_type

        # LLM 决策
        new_state = await self._think(state, now, persona_id)
        new_state.updated_at = now.isoformat()
        await self._save_state(persona_id, new_state)

        # 状态变化时触发副作用
        if new_state.activity_type != old_activity:
            await self._on_state_change(persona_id, old_activity, new_state)

    async def _think(
        self, state: LifeState, now: datetime, persona_id: str
    ) -> LifeState:
        """调用 LLM 决定下一步状态"""
        from app.agents.infra.langfuse_client import get_prompt
        from app.agents.infra.model_builder import ModelBuilder
        from app.config.config import settings
        from app.orm.crud import get_bot_persona, get_plan_for_period
        from app.orm.memory_crud import get_today_fragments

        persona = await get_bot_persona(persona_id)
        persona_name = persona.display_name if persona else persona_id
        persona_lite = persona.persona_lite if persona else ""

        # 今日 Schedule
        today = now.strftime("%Y-%m-%d")
        schedule = await get_plan_for_period("daily", today, today, persona_id)
        schedule_text = schedule.content if schedule else "（今天还没有安排）"

        # 最近经历碎片（给 LLM 知道今天发生了什么）
        today_frags = await get_today_fragments(persona_id, grains=["conversation", "glimpse"])
        frag_text = "\n".join(f.content[:100] for f in today_frags[-5:]) if today_frags else "（今天还没什么经历）"

        prompt = get_prompt("life_engine_tick")
        compiled = prompt.compile(
            persona_name=persona_name,
            persona_lite=persona_lite,
            current_time=now.strftime("%H:%M"),
            current_state=state.current_state,
            activity_type=state.activity_type,
            response_mood=state.response_mood,
            schedule=schedule_text,
            recent_experiences=frag_text,
        )

        model = await ModelBuilder.build_chat_model(settings.life_engine_model)
        response = await model.ainvoke([{"role": "user", "content": compiled}])
        raw = _extract_text(response.content)

        return self._parse_tick_response(raw, state, now)

    def _parse_tick_response(
        self, raw: str, fallback: LifeState, now: datetime
    ) -> LifeState:
        """解析 LLM tick 响应为 LifeState"""
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(raw[start:end])
                skip_minutes = data.get("skip_minutes", 0)
                skip_until = None
                if skip_minutes and int(skip_minutes) > 0:
                    skip_until = (now + timedelta(minutes=int(skip_minutes))).isoformat()

                activity = data.get("activity_type", "idle")
                valid_types = {"browsing", "sleeping", "out", "busy", "idle"}
                if activity not in valid_types:
                    activity = "idle"

                return LifeState(
                    current_state=data.get("current_state", fallback.current_state),
                    activity_type=activity,
                    response_mood=data.get("response_mood", fallback.response_mood),
                    skip_until=skip_until,
                    updated_at=now.isoformat(),
                )
        except (json.JSONDecodeError, ValueError, TypeError) as e:
            logger.warning(f"Failed to parse tick response: {e}, raw={raw[:200]}")

        return fallback

    async def _on_state_change(
        self, persona_id: str, old_activity: str, new_state: LifeState
    ) -> None:
        """状态变化时的副作用"""
        logger.info(
            f"[{persona_id}] State change: {old_activity} → {new_state.activity_type} "
            f"({new_state.current_state[:40]})"
        )
        if new_state.activity_type == "browsing":
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
