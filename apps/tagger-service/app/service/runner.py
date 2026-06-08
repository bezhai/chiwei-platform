from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app.pipeline.merge import dedup_ids, merge_row

logger = logging.getLogger(__name__)


class PersistentStageRunner:
    """Run sync GPU stages serially while keeping loaded models warm between batches."""

    def __init__(self, stages: list[Any], *, idle_unload_seconds: float | None) -> None:
        self._stages = stages
        self._idle_unload_seconds = idle_unload_seconds
        self._lock = asyncio.Lock()
        self._loaded = False
        self._idle_task: asyncio.Task[None] | None = None
        self._generation = 0

    @property
    def loaded(self) -> bool:
        return self._loaded

    async def preload(self) -> None:
        async with self._lock:
            self._cancel_idle_timer()
            try:
                await asyncio.to_thread(self._ensure_loaded_sync)
            except Exception:
                logger.exception("stage runner preload failed; unloading stages before surfacing error")
                await asyncio.to_thread(self._unload_sync)
                raise
            finally:
                self._generation += 1
                self._schedule_idle_unload(self._generation)

    async def run(self, items: list[tuple[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        async with self._lock:
            self._cancel_idle_timer()
            try:
                rows, dups = await asyncio.to_thread(self._run_sync, items)
            except Exception:
                logger.exception("stage runner failed; unloading stages before surfacing error")
                await asyncio.to_thread(self._unload_sync)
                raise
            finally:
                self._generation += 1
                self._schedule_idle_unload(self._generation)
            return rows, dups

    async def unload(self) -> None:
        async with self._lock:
            self._cancel_idle_timer()
            await asyncio.to_thread(self._unload_sync)
            self._generation += 1

    def _run_sync(self, items: list[tuple[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        kept, dups = dedup_ids(items)
        self._ensure_loaded_sync()
        merged: dict[str, dict[str, dict[str, Any]]] = {image_id: {} for image_id, _ in kept}
        for stage in self._stages:
            stage_out = stage.run(kept)
            for image_id, caps in stage_out.items():
                merged[image_id].update(caps)
        rows = [merge_row(image_id, merged[image_id]) for image_id, _ in kept]
        return rows, dups

    def _ensure_loaded_sync(self) -> None:
        if self._loaded:
            return
        loaded: list[Any] = []
        try:
            for stage in self._stages:
                stage.load()
                loaded.append(stage)
        except Exception:
            for stage in reversed(loaded):
                with contextlib.suppress(Exception):
                    stage.unload()
            raise
        self._loaded = True

    def _unload_sync(self) -> None:
        if not self._loaded:
            return
        for stage in reversed(self._stages):
            with contextlib.suppress(Exception):
                stage.unload()
        self._loaded = False

    def _cancel_idle_timer(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None

    def _schedule_idle_unload(self, generation: int) -> None:
        if self._idle_unload_seconds is None or self._idle_unload_seconds <= 0:
            return
        self._idle_task = asyncio.create_task(self._idle_unload_after_delay(generation))

    async def _idle_unload_after_delay(self, generation: int) -> None:
        try:
            await asyncio.sleep(self._idle_unload_seconds)
            async with self._lock:
                if generation == self._generation:
                    await asyncio.to_thread(self._unload_sync)
        except asyncio.CancelledError:
            return
