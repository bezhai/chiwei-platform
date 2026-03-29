"""赤尾 Identity 漂移状态机

两阶段锁模型：
  一阶段（可中断）：收集消息，debounce N 秒，超过 M 条强制 flush
  二阶段（不可中断）：LLM 漂移计算，更新 identity 状态

每个群/私聊维护独立的漂移锁。
"""

import logging
from datetime import datetime, timedelta, timezone

from app.clients.redis import AsyncRedisClient
from app.config.config import settings

logger = logging.getLogger(__name__)

CST = timezone(timedelta(hours=8))

# Redis key 前缀
_KEY_PREFIX = "identity"


def _state_key(chat_id: str) -> str:
    return f"{_KEY_PREFIX}:{chat_id}"


async def get_identity_state(chat_id: str) -> str | None:
    """从 Redis 读取当前 identity 漂移状态"""
    redis = AsyncRedisClient.get_instance()
    return await redis.hget(_state_key(chat_id), "state")


async def get_identity_updated_at(chat_id: str) -> str | None:
    """读取上次漂移更新时间（ISO 格式）"""
    redis = AsyncRedisClient.get_instance()
    return await redis.hget(_state_key(chat_id), "updated_at")


async def set_identity_state(chat_id: str, state: str) -> None:
    """写入 identity 漂移状态到 Redis"""
    redis = AsyncRedisClient.get_instance()
    now = datetime.now(CST).isoformat()
    pipe = redis.pipeline()
    pipe.hset(_state_key(chat_id), mapping={"state": state, "updated_at": now})
    pipe.expire(_state_key(chat_id), settings.identity_drift_ttl_seconds)
    await pipe.execute()
    logger.info(f"Identity state updated for {chat_id}: {state[:50]}...")
