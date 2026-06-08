from __future__ import annotations

import asyncio
from typing import Any

from app.service.image_loader import ObjectReader, load_images_from_paths
from app.service.results import dedup_paths, merge_rows_for_paths
from app.service.runner import PersistentStageRunner


class LocalInferenceService:
    def __init__(self, *, reader: ObjectReader, runner: PersistentStageRunner) -> None:
        self._reader = reader
        self._runner = runner

    @property
    def loaded(self) -> bool:
        return self._runner.loaded

    async def preload(self) -> None:
        await self._runner.preload()

    async def infer_paths(self, paths: list[str]) -> dict[str, Any]:
        unique_paths, dups = dedup_paths(paths)
        loaded = await asyncio.to_thread(load_images_from_paths, unique_paths, self._reader)
        rows: list[dict[str, Any]] = []
        if loaded.items:
            rows, runner_dups = await self._runner.run(loaded.items)
            dups.extend(runner_dups)
        merged = merge_rows_for_paths(unique_paths, rows, loaded.error_rows)
        return {"rows": merged, "dups": dups}

    async def unload(self) -> None:
        await self._runner.unload()
