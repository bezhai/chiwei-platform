"""DebouncedPipeline — two-phase debounce base class for memory pipelines.

Two-phase lock model:
  Phase 1 (interruptible): collect events, debounce N seconds, force flush at M events.
  Phase 2 (non-interruptible): subclass executes ``process()``.

Each ``(chat_id, persona_id)`` pair is managed independently.
Afterthought and drift share this state machine; only ``process()`` differs.
"""

import asyncio
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class DebouncedPipeline(ABC):
    """Two-phase debounce pipeline.

    Subclasses implement ``process()`` only; buffer / timer / phase-2 lock
    logic lives here.
    """

    def __init__(self, debounce_seconds: float, max_buffer: int) -> None:
        self._debounce_seconds = debounce_seconds
        self._max_buffer = max_buffer
        self._buffers: dict[str, int] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._phase2_running: set[str] = set()

    # ------------------------------------------------------------------

    @staticmethod
    def _key(chat_id: str, persona_id: str) -> str:
        return f"{chat_id}:{persona_id}"

    async def on_event(self, chat_id: str, persona_id: str) -> None:
        """Receive an event and enter the two-phase flow."""
        key = self._key(chat_id, persona_id)
        self._buffers[key] = self._buffers.get(key, 0) + 1
        logger.info(
            "%s on_event: chat_id=%s, persona=%s, buffer=%d, phase2_running=%s",
            self.__class__.__name__,
            chat_id,
            persona_id,
            self._buffers[key],
            key in self._phase2_running,
        )

        # Phase 2 running -> buffer only, do not trigger
        if key in self._phase2_running:
            return

        # Cancel existing timer (reset debounce)
        if key in self._timers:
            self._timers[key].cancel()
            del self._timers[key]

        # Over threshold -> force phase 2
        if self._buffers.get(key, 0) >= self._max_buffer:
            asyncio.create_task(self._enter_phase2(chat_id, persona_id))
            return

        # Start / reset debounce timer
        self._timers[key] = asyncio.create_task(
            self._phase1_timer(chat_id, persona_id)
        )

    async def _phase1_timer(self, chat_id: str, persona_id: str) -> None:
        """Phase 1 timer: enter phase 2 after N seconds of silence."""
        try:
            await asyncio.sleep(self._debounce_seconds)
            await self._enter_phase2(chat_id, persona_id)
        except asyncio.CancelledError:
            pass  # timer reset by new event

    async def _enter_phase2(self, chat_id: str, persona_id: str) -> None:
        """Enter phase 2: drain buffer, run ``process()``."""
        key = self._key(chat_id, persona_id)
        event_count = self._buffers.pop(key, 0)
        self._timers.pop(key, None)

        if event_count == 0:
            return

        self._phase2_running.add(key)
        try:
            logger.info(
                "%s phase2 for %s persona=%s: %d events buffered",
                self.__class__.__name__,
                chat_id,
                persona_id,
                event_count,
            )
            await self.process(chat_id, persona_id, event_count)
        except Exception:
            logger.exception(
                "%s process failed for %s persona=%s",
                self.__class__.__name__,
                chat_id,
                persona_id,
            )
        finally:
            self._phase2_running.discard(key)
            # New events during phase 2 -> start next cycle
            if self._buffers.get(key, 0) > 0:
                asyncio.create_task(self.on_event(chat_id, persona_id))

    @abstractmethod
    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        """Subclass hook — batch processing logic.

        Args:
            chat_id: group / p2p chat ID
            persona_id: persona ID
            event_count: number of events accumulated this cycle
        """
        ...
