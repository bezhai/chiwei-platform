"""MVP 打标 harness：从本地缓存图构造 (id, 图) → 按模型阶段串行跑 → 每 id 一行合并结果 jsonl。

本地缓存当测试夹具（spec）：用 assets.jsonl 定位 local_path 加载图、pixiv_addr 作 id；打标器本身
只收 (id, 图)、不碰 assets。阶段顺序：QwenVllmStage（GPU，describe+OCR 共享一个 vLLM 实例）→
TaggerStage（wd14/eva02 onnx + anime_rating + phash）；Qwen 卸载后 tagger 阶段才 load、不抢显存。
重依赖（vllm/onnx/imgutils）都在函数内 import，本机 import 本模块、跑 load_items 测试不触发 GPU。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from PIL import Image


def load_items(assets: list[dict[str, Any]], limit: int = 0) -> list[tuple[str, Image.Image]]:
    """从 asset 的 local_path 加载图、pixiv_addr 作 id 构造 (id, PIL)；坏图/缺图/缺路径跳过不崩。

    一次性把 limit 张图全 load 进内存（img.load() 强制解码）。limit 控制批大小：生产大批量须
    调用方自行分批喂 run_pipeline，别一次 load 上千张——整批解码图 + 下游整批推理输入会吃爆 RAM。
    """
    selected = assets[:limit] if limit > 0 else assets
    items: list[tuple[str, Image.Image]] = []
    for asset in selected:
        image_id = asset.get("pixiv_addr")
        path = asset.get("local_path")
        if not image_id or not path:
            continue
        try:
            img = Image.open(path)
            img.load()  # 强制读取像素，及早暴露坏图（Image.open 是惰性的）
        except Exception as exc:
            print(f"[skip] {image_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        items.append((image_id, img))
    return items


def build_stages(
    model_path: str,
    *,
    with_qwen: bool = True,
    with_taggers: bool = True,
    wd14_model_dir: Path | None = None,
    eva02_model_dir: Path | None = None,
) -> list[Any]:
    """组装阶段：Qwen（GPU describe+OCR）+ TaggerStage（onnx/CPU 打标器，工厂延迟构造）。"""
    from app.pipeline.orchestrate import TaggerStage

    stages: list[Any] = []
    if with_qwen:
        from app.pipeline.qwen_stage import QwenVllmStage

        stages.append(QwenVllmStage(model_path))
    if with_taggers:
        from app.pipeline.cpu_taggers import AnimeRatingTagger, PHashTagger
        from app.pipeline.wd14_tagger import Wd14Tagger

        stages.append(TaggerStage([
            lambda: Wd14Tagger(
                "wd14",
                model_repo="SmilingWolf/wd-vit-tagger-v3",
                model_dir=wd14_model_dir,
            ),
            lambda: Wd14Tagger(
                "eva02",
                model_repo="SmilingWolf/wd-eva02-large-tagger-v3",
                model_dir=eva02_model_dir,
            ),
            lambda: AnimeRatingTagger(),
            lambda: PHashTagger(),
        ]))
    return stages


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True, help="本地缓存图的 assets.jsonl（夹具）")
    parser.add_argument("--out", type=Path, default=Path("data/raw/mvp_pipeline.jsonl"))
    parser.add_argument(
        "--model",
        default=os.getenv("TAGGER_QWEN_MODEL_PATH", ""),
    )
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--no-qwen", action="store_true", help="跳过 GPU describe/OCR 阶段（只验 tagger 阶段）")
    parser.add_argument("--no-taggers", action="store_true", help="跳过 tagger 阶段（只验 Qwen 阶段）")
    args = parser.parse_args()
    if not args.no_qwen and not args.model:
        parser.error("--model or TAGGER_QWEN_MODEL_PATH is required unless --no-qwen is set")

    from app.pipeline.orchestrate import run_pipeline

    assets = read_jsonl(args.assets)
    items = load_items(assets, limit=args.limit)
    print(f"[load] {len(items)} images", file=sys.stderr, flush=True)
    stages = build_stages(args.model, with_qwen=not args.no_qwen, with_taggers=not args.no_taggers)
    rows, dups = run_pipeline(items, stages)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[done] wrote {len(rows)} rows to {args.out} (dups={len(dups)})", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
