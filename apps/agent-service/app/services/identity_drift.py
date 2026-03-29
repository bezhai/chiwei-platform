"""赤尾 Identity 漂移状态机

两阶段锁模型：
  一阶段（可中断）：收集消息，debounce N 秒，超过 M 条强制 flush
  二阶段（不可中断）：LLM 漂移计算，更新 identity 状态

每个群/私聊维护独立的漂移锁。
"""

import asyncio
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


class IdentityDriftManager:
    """两阶段锁 identity 漂移管理器

    每个 chat_id 独立管理，不并行漂移。
    一阶段：收集消息（debounce N 秒 + 强制 flush M 条）
    二阶段：LLM 漂移计算（不可中断）
    """

    _instance: "IdentityDriftManager | None" = None

    def __init__(self):
        self._buffers: dict[str, int] = {}  # chat_id -> event count
        self._timers: dict[str, asyncio.Task] = {}  # chat_id -> phase1 timer
        self._phase2_running: set[str] = set()  # chat_ids in phase2

    @classmethod
    def get_instance(cls) -> "IdentityDriftManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def on_event(self, chat_id: str) -> None:
        """消息/回复事件 -> 进入两阶段锁流程"""
        self._buffers[chat_id] = self._buffers.get(chat_id, 0) + 1

        # 二阶段运行中 -> 只缓冲，不触发
        if chat_id in self._phase2_running:
            return

        # 取消已有计时器（重置 debounce）
        if chat_id in self._timers:
            self._timers[chat_id].cancel()
            del self._timers[chat_id]

        # 超过阈值 -> 强制进入二阶段
        if self._buffers.get(chat_id, 0) >= settings.identity_drift_max_buffer:
            asyncio.create_task(self._enter_phase2(chat_id))
            return

        # 启动/重置 debounce 计时器
        self._timers[chat_id] = asyncio.create_task(
            self._phase1_timer(chat_id)
        )

    async def _phase1_timer(self, chat_id: str):
        """一阶段计时器：N 秒无新消息后进入二阶段"""
        try:
            await asyncio.sleep(settings.identity_drift_debounce_seconds)
            await self._enter_phase2(chat_id)
        except asyncio.CancelledError:
            pass  # timer reset by new event

    async def _enter_phase2(self, chat_id: str):
        """进入二阶段：清空缓冲区，执行 LLM 漂移"""
        event_count = self._buffers.pop(chat_id, 0)
        self._timers.pop(chat_id, None)

        if event_count == 0:
            return

        self._phase2_running.add(chat_id)
        try:
            logger.info(
                f"Identity drift phase2 for {chat_id}: "
                f"{event_count} events buffered"
            )
            await _run_drift(chat_id)
        except Exception as e:
            logger.error(f"Identity drift failed for {chat_id}: {e}")
        finally:
            self._phase2_running.discard(chat_id)
            # 二阶段期间有新事件 -> 启动下一轮
            if self._buffers.get(chat_id, 0) > 0:
                asyncio.create_task(self.on_event(chat_id))


async def _run_drift(chat_id: str) -> None:
    """二阶段：LLM 漂移计算（占位，Task 4 实现）"""
    pass
