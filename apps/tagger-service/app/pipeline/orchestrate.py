"""编排：一批 (id, 图) → 按模型阶段串行（每阶段 load→过整批→unload）→ 各阶段结果按 id 合并成一行。

阶段（stage）统一协议 load() / run(items)->{id:{name:result}} / unload()：
- QwenVllmStage（qwen_stage 模块）：一个 vLLM 实例独占显存跑 describe+OCR，跑完卸载释放。
- TaggerStage：一组 .tag(image) 打标器（wd14/eva02/anime_rating/phash），工厂延迟构造——
  占显存的 onnx 等到本阶段 load 才 load，不和 Qwen 抢显存。
每阶段必 unload（即便 run 抛异常），保证显存不在阶段间泄漏。单打标器/单图异常隔离、不崩整批。
"""
from __future__ import annotations

import gc
from typing import Any, Callable

from app.pipeline.merge import dedup_ids, merge_row


class TaggerStage:
    """把一组 .tag(image) 打标器包成阶段：load 构造打标器、run 每图过所有、unload 释放。

    打标器用工厂延迟构造——占显存的 onnx（wd14/eva02）等本阶段 load 才 load、不和 Qwen 抢显存。
    单打标器对某图抛异常时兜一层进该能力 error、不拖垮该图其余能力、不崩整批。
    """

    def __init__(self, factories: list[Callable[[], Any]]) -> None:
        self._factories = factories
        self._taggers: list[Any] = []

    def load(self) -> None:
        self._taggers = [factory() for factory in self._factories]

    def run(self, items: list[tuple[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
        out: dict[str, dict[str, dict[str, Any]]] = {}
        for image_id, image in items:
            caps: dict[str, dict[str, Any]] = {}
            for tagger in self._taggers:
                try:
                    caps[tagger.name] = tagger.tag(image)
                except Exception as exc:
                    caps[tagger.name] = {"error": f"{type(exc).__name__}: {exc}"}
            out[image_id] = caps
        return out

    def unload(self) -> None:
        self._taggers = []
        gc.collect()


def run_pipeline(
    items: list[tuple[str, Any]], stages: list[Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    """按阶段串行驱动：每阶段 load→run(整批)→unload，各阶段产出按 id 合并成一行。

    去重在此统一做一次（各阶段收到去重后的 items、内部不再去重）。某阶段 run 抛异常仍 unload
    后再上抛（显存不泄漏）；正常路径下各能力结果汇总进 merge_row、缺失能力留空、单能力 error 进 errors。
    """
    kept, dups = dedup_ids(items)
    merged: dict[str, dict[str, dict[str, Any]]] = {image_id: {} for image_id, _ in kept}
    for stage in stages:
        stage.load()
        try:
            stage_out = stage.run(kept)
        finally:
            stage.unload()
        for image_id, caps in stage_out.items():
            merged[image_id].update(caps)
    rows = [merge_row(image_id, merged[image_id]) for image_id, _ in kept]
    return rows, dups
