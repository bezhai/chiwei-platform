"""DebouncedPipeline — 两阶段 debounce 管线基类

两阶段锁模型：
  一阶段（可中断）：收集事件，debounce N 秒，超过 M 条强制 flush
  二阶段（不可中断）：子类执行 process()

每个 (chat_id, persona_id) 组合独立管理。
AfterthoughtManager 和 IdentityDriftManager 共享此状态机，仅 process() 不同。
"""

import asyncio
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class DebouncedPipeline(ABC):
    """两阶段 debounce 管线

    子类只需实现 process()，buffer/timer/phase2 锁逻辑由基类管理。
    """

    def __init__(self, debounce_seconds: float, max_buffer: int):
        self._debounce_seconds = debounce_seconds
        self._max_buffer = max_buffer
        self._buffers: dict[str, int] = {}  # "chat_id:persona_id" -> event count
        self._timers: dict[str, asyncio.Task] = {}  # "chat_id:persona_id" -> phase1 timer
        self._phase2_running: set[str] = set()  # keys currently in phase2

    def _key(self, chat_id: str, persona_id: str) -> str:
        return f"{chat_id}:{persona_id}"

    async def on_event(self, chat_id: str, persona_id: str) -> None:
        """收到事件 -> 进入两阶段锁流程"""
        key = self._key(chat_id, persona_id)
        self._buffers[key] = self._buffers.get(key, 0) + 1
        logger.info(
            f"{self.__class__.__name__} on_event: chat_id={chat_id}, "
            f"persona={persona_id}, buffer={self._buffers[key]}, "
            f"phase2_running={key in self._phase2_running}"
        )

        # 二阶段运行中 -> 只缓冲，不触发
        if key in self._phase2_running:
            return

        # 取消已有计时器（重置 debounce）
        if key in self._timers:
            self._timers[key].cancel()
            del self._timers[key]

        # 超过阈值 -> 强制进入二阶段
        if self._buffers.get(key, 0) >= self._max_buffer:
            asyncio.create_task(self._enter_phase2(chat_id, persona_id))
            return

        # 启动/重置 debounce 计时器
        self._timers[key] = asyncio.create_task(
            self._phase1_timer(chat_id, persona_id)
        )

    async def _phase1_timer(self, chat_id: str, persona_id: str) -> None:
        """一阶段计时器：N 秒无新消息后进入二阶段"""
        try:
            await asyncio.sleep(self._debounce_seconds)
            await self._enter_phase2(chat_id, persona_id)
        except asyncio.CancelledError:
            pass  # timer reset by new event

    async def _enter_phase2(self, chat_id: str, persona_id: str) -> None:
        """进入二阶段：清空缓冲区，执行 process()"""
        key = self._key(chat_id, persona_id)
        event_count = self._buffers.pop(key, 0)
        self._timers.pop(key, None)

        if event_count == 0:
            return

        self._phase2_running.add(key)
        try:
            logger.info(
                f"{self.__class__.__name__} phase2 for {chat_id} "
                f"persona={persona_id}: {event_count} events buffered"
            )
            await self.process(chat_id, persona_id, event_count)
        except Exception as e:
            logger.error(
                f"{self.__class__.__name__} process failed for "
                f"{chat_id} persona={persona_id}: {e}"
            )
        finally:
            self._phase2_running.discard(key)
            # 二阶段期间有新事件 -> 启动下一轮
            if self._buffers.get(key, 0) > 0:
                asyncio.create_task(self.on_event(chat_id, persona_id))

    @abstractmethod
    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        """子类实现：批量处理逻辑

        Args:
            chat_id: 群/私聊 ID
            persona_id: 角色 ID
            event_count: 本轮累积的事件数量
        """
        ...
