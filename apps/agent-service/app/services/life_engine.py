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
