"""消息级主动搭话管理器 — debounce + 每小时硬限

替代 cron 扫描，由 lark-server 每条群消息发 MQ 事件触发。
debounce 攒消息 → hourly limit 检查 → 调用 proactive_scanner 判断 + 投递。

频率克制（"最近主动说了但没人搭理"）交给小模型通过 recent_proactive 上下文判断，
不做工程硬编码的 cooldown 或 consecutive 计数。
"""

import asyncio
import logging

from app.clients.redis import AsyncRedisClient
from app.workers.proactive_scanner import TARGET_CHAT_ID, run_proactive_scan

logger = logging.getLogger(__name__)

TARGET_CHAT_IDS = {TARGET_CHAT_ID}


class ProactiveManager:
    _instance: "ProactiveManager | None" = None

    DEBOUNCE_SECONDS = 15
    HOURLY_LIMIT = 6
    HOURLY_COUNT_KEY = "proactive:hourly_count:{chat_id}"  # TTL 1h

    def __init__(self) -> None:
        self._timers: dict[str, asyncio.Task] = {}
        self._scanning: set[str] = set()

    @classmethod
    def get_instance(cls) -> "ProactiveManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def on_event(self, chat_id: str) -> None:
        """每条群消息调用一次，debounce 后触发扫描"""
        if chat_id not in TARGET_CHAT_IDS:
            return
        if chat_id in self._timers:
            self._timers[chat_id].cancel()
        self._timers[chat_id] = asyncio.create_task(self._debounce_timer(chat_id))

    async def _debounce_timer(self, chat_id: str) -> None:
        try:
            await asyncio.sleep(self.DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        finally:
            self._timers.pop(chat_id, None)
        await self._execute_scan(chat_id)

    async def _execute_scan(self, chat_id: str) -> None:
        if chat_id in self._scanning:
            return
        self._scanning.add(chat_id)
        try:
            if not await self._check_hourly_limit(chat_id):
                logger.info("proactive: hourly limit reached for %s", chat_id)
                return
            result = await run_proactive_scan(source="message_event")
            if "submitted" in result:
                await self._incr_hourly(chat_id)
        except Exception:
            logger.exception("proactive _execute_scan error for %s", chat_id)
        finally:
            self._scanning.discard(chat_id)

    async def _check_hourly_limit(self, chat_id: str) -> bool:
        redis = AsyncRedisClient.get_instance()
        val = await redis.get(self.HOURLY_COUNT_KEY.format(chat_id=chat_id))
        return val is None or int(val) < self.HOURLY_LIMIT

    async def _incr_hourly(self, chat_id: str) -> None:
        redis = AsyncRedisClient.get_instance()
        key = self.HOURLY_COUNT_KEY.format(chat_id=chat_id)
        new_val = await redis.incr(key)
        if new_val == 1:
            await redis.expire(key, 3600)
