"""消息级主动搭话管理器 — debounce + 频率控制 + 连续无回应检测

替代 cron 扫描，由 lark-server 每条群消息发 MQ 事件触发。
debounce 攒消息 → 硬限检查 → 调用 proactive_scanner 判断 + 投递。
"""

import asyncio
import logging
import time

from sqlalchemy import select, func as sa_func

from app.clients.redis import AsyncRedisClient
from app.orm.base import AsyncSessionLocal
from app.orm.models import ConversationMessage
from app.workers.proactive_scanner import PROACTIVE_USER_ID, TARGET_CHAT_ID, run_proactive_scan

logger = logging.getLogger(__name__)

# 只对目标群生效（后续可扩展为集合）
TARGET_CHAT_IDS = {TARGET_CHAT_ID}


class ProactiveManager:
    """消息级主动搭话管理器 — debounce + 小模型判断"""

    _instance: "ProactiveManager | None" = None

    DEBOUNCE_SECONDS = 15  # 攒消息的等待时间
    HOURLY_LIMIT = 6  # 每小时最多主动发言次数
    CONSECUTIVE_LIMIT = 2  # 连续无人回应次数上限
    CONSECUTIVE_COOLDOWN = 3 * 3600  # 连续无回应后冷却 3 小时

    # Redis keys
    HOURLY_COUNT_KEY = "proactive:hourly_count:{chat_id}"  # TTL 1h
    CONSECUTIVE_KEY = "proactive:consecutive_noreply:{chat_id}"
    COOLDOWN_KEY = "proactive:cooldown:{chat_id}"  # TTL varies
    LAST_PROACTIVE_TS_KEY = "proactive:last_ts:{chat_id}"

    def __init__(self) -> None:
        self._timers: dict[str, asyncio.Task] = {}
        self._scanning: set[str] = set()  # 防止并发扫描

    @classmethod
    def get_instance(cls) -> "ProactiveManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── 公开入口 ───────────────────────────────────────────────────────────

    async def on_event(self, chat_id: str) -> None:
        """每条群消息调用一次，debounce 后触发扫描"""
        if chat_id not in TARGET_CHAT_IDS:
            return
        redis = AsyncRedisClient.get_instance()

        # 冷却中则跳过
        if await redis.exists(self.COOLDOWN_KEY.format(chat_id=chat_id)):
            return

        # 取消已有计时器（重置 debounce）
        if chat_id in self._timers:
            self._timers[chat_id].cancel()

        self._timers[chat_id] = asyncio.create_task(
            self._debounce_timer(chat_id)
        )

    # ── 内部方法 ───────────────────────────────────────────────────────────

    async def _debounce_timer(self, chat_id: str) -> None:
        """等待 DEBOUNCE_SECONDS 后执行扫描"""
        try:
            await asyncio.sleep(self.DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            logger.debug("proactive debounce cancelled for %s", chat_id)
            return
        finally:
            self._timers.pop(chat_id, None)

        logger.debug("proactive debounce fired for %s", chat_id)
        await self._execute_scan(chat_id)

    async def _execute_scan(self, chat_id: str) -> None:
        """实际扫描：硬限检查 → 连续无回应检测 → 调用 scanner"""
        if chat_id in self._scanning:
            logger.debug("proactive scan already running for %s, skip", chat_id)
            return

        self._scanning.add(chat_id)
        try:
            # 1. 每小时频次检查
            if not await self._check_hourly_limit(chat_id):
                logger.info("proactive: hourly limit reached for %s", chat_id)
                return

            # 2. 连续无回应检查 + 重置
            if not await self._check_consecutive_limit(chat_id):
                logger.info("proactive: consecutive no-reply limit for %s", chat_id)
                return
            await self._check_and_reset_consecutive(chat_id)

            # 3. 调用 scanner
            result = await run_proactive_scan(source="message_event")

            if "submitted" in result:
                await self._update_after_submit(chat_id)
        except Exception:
            logger.exception("proactive _execute_scan error for %s", chat_id)
        finally:
            self._scanning.discard(chat_id)

    async def _check_hourly_limit(self, chat_id: str) -> bool:
        """检查每小时发言次数是否在限制内（不递增）"""
        redis = AsyncRedisClient.get_instance()
        key = self.HOURLY_COUNT_KEY.format(chat_id=chat_id)
        val = await redis.get(key)
        if val is not None and int(val) >= self.HOURLY_LIMIT:
            return False
        return True

    async def _check_consecutive_limit(self, chat_id: str) -> bool:
        """检查连续无回应次数是否在限制内"""
        redis = AsyncRedisClient.get_instance()
        key = self.CONSECUTIVE_KEY.format(chat_id=chat_id)
        val = await redis.get(key)
        if val is not None and int(val) >= self.CONSECUTIVE_LIMIT:
            # 触发长冷却
            await self._set_cooldown(chat_id, self.CONSECUTIVE_COOLDOWN)
            # 重置计数器
            await redis.delete(key)
            return False
        return True

    async def _check_and_reset_consecutive(self, chat_id: str) -> None:
        """扫描前检查：上次主动发言后是否有人回应，有则重置 consecutive 计数和冷却"""
        redis = AsyncRedisClient.get_instance()
        ts_key = self.LAST_PROACTIVE_TS_KEY.format(chat_id=chat_id)
        last_ts = await redis.get(ts_key)
        if last_ts is None:
            return

        last_ts_ms = int(last_ts)

        async with AsyncSessionLocal() as session:
            stmt = (
                select(sa_func.count())
                .select_from(ConversationMessage)
                .where(
                    ConversationMessage.chat_id == chat_id,
                    ConversationMessage.role == "user",
                    ConversationMessage.user_id != PROACTIVE_USER_ID,
                    ConversationMessage.create_time > last_ts_ms,
                )
            )
            result = await session.execute(stmt)
            count = result.scalar() or 0

        if count > 0:
            # 有人说话了，重置连续无回应计数 + 清除冷却
            await redis.delete(self.CONSECUTIVE_KEY.format(chat_id=chat_id))
            await redis.delete(self.COOLDOWN_KEY.format(chat_id=chat_id))

    async def _update_after_submit(self, chat_id: str) -> None:
        """主动发言成功后：递增每小时计数 + 递增连续计数 + 记录时间戳"""
        redis = AsyncRedisClient.get_instance()
        now_ms = int(time.time() * 1000)

        # 每小时计数 INCR（首次设置 1h TTL）
        hourly_key = self.HOURLY_COUNT_KEY.format(chat_id=chat_id)
        new_val = await redis.incr(hourly_key)
        if new_val == 1:
            await redis.expire(hourly_key, 3600)

        # 连续无回应计数 INCR
        cons_key = self.CONSECUTIVE_KEY.format(chat_id=chat_id)
        await redis.incr(cons_key)

        # 记录最后主动发言时间戳
        ts_key = self.LAST_PROACTIVE_TS_KEY.format(chat_id=chat_id)
        await redis.set(ts_key, str(now_ms), ex=86400)  # 24h TTL

    async def _set_cooldown(self, chat_id: str, seconds: int) -> None:
        """设置冷却期"""
        redis = AsyncRedisClient.get_instance()
        key = self.COOLDOWN_KEY.format(chat_id=chat_id)
        await redis.set(key, "1", ex=seconds)
        logger.info("proactive: cooldown set for %s, %ds", chat_id, seconds)
