from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app.pipeline.merge import dedup_ids, merge_row

logger = logging.getLogger(__name__)


class PersistentStageRunner:
    """串行跑同步阶段，并让已 load 的阶段在批次之间保持热。

    第一批到达时才 load（懒加载），之后一直复用；只有 run 抛异常或进程退出时才 unload——
    异常路径卸载是为了不把半死的阶段状态带进下一批。
    """

    def __init__(self, stages: list[Any]) -> None:
        self._stages = stages
        self._lock = asyncio.Lock()
        self._loaded = False

    async def run(self, items: list[tuple[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        async with self._lock:
            try:
                return await asyncio.to_thread(self._run_sync, items)
            except Exception:
                logger.exception("stage runner failed; unloading stages before surfacing error")
                await asyncio.to_thread(self._unload_sync)
                raise

    async def unload(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._unload_sync)

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
