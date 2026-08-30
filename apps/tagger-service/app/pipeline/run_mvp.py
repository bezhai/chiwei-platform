"""MVP 打标 harness：从本地缓存图构造 (id, 图) → 按模型阶段串行跑 → 每 id 一行合并结果 jsonl。

本地缓存当测试夹具（spec）：用 assets.jsonl 定位 local_path 加载图、pixiv_addr 作 id；打标器本身
只收 (id, 图)、不碰 assets。阶段顺序：QwenVlHttpStage（HTTP 调 llama-swap 拿 describe+OCR）→
TaggerStage（wd14/eva02 onnx + anime_rating + phash）。onnx/imgutils 这些重依赖在函数内 import，
本机 import 本模块、跑 load_items 测试不触发 GPU。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from PIL import Image

from app.pipeline.qwen_stage import QwenVlEndpoint, QwenVlHttpStage


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
    *,
    qwen: QwenVlEndpoint | None,
    with_taggers: bool = True,
    wd14_model_dir: Path | None = None,
    eva02_model_dir: Path | None = None,
) -> list[Any]:
    """组装阶段：Qwen（HTTP describe+OCR）+ TaggerStage（onnx/CPU 打标器，工厂延迟构造）。"""
    from app.pipeline.orchestrate import TaggerStage

    stages: list[Any] = []
    if qwen is not None:
        stages.append(QwenVlHttpStage(qwen))
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
        "--qwen-base-url",
        default=os.getenv("TAGGER_QWEN_BASE_URL", ""),
        help="llama-swap 的 OpenAI 兼容根地址，含 /v1 前缀",
    )
    parser.add_argument("--qwen-model", default=os.getenv("TAGGER_QWEN_MODEL", ""))
    parser.add_argument("--qwen-concurrency", type=int, default=2, help="并发图片数，对齐服务端 slot 数")
    parser.add_argument("--qwen-timeout", type=float, default=180.0)
    parser.add_argument("--qwen-max-vision-tokens", type=int, default=4096)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--no-qwen", action="store_true", help="跳过 describe/OCR 阶段（只验 tagger 阶段）")
    parser.add_argument("--no-taggers", action="store_true", help="跳过 tagger 阶段（只验 Qwen 阶段）")
    args = parser.parse_args()
    if not args.no_qwen and not (args.qwen_base_url and args.qwen_model):
        parser.error(
            "--qwen-base-url/--qwen-model (or TAGGER_QWEN_BASE_URL/TAGGER_QWEN_MODEL) "
            "are required unless --no-qwen is set"
        )

    from app.pipeline.orchestrate import run_pipeline

    qwen = (
        None
        if args.no_qwen
        else QwenVlEndpoint(
            base_url=args.qwen_base_url,
            model=args.qwen_model,
            api_key=os.getenv("TAGGER_QWEN_API_KEY", ""),
            timeout_seconds=args.qwen_timeout,
            concurrency=args.qwen_concurrency,
            max_vision_tokens=args.qwen_max_vision_tokens,
        )
    )
    assets = read_jsonl(args.assets)
    items = load_items(assets, limit=args.limit)
    print(f"[load] {len(items)} images", file=sys.stderr, flush=True)
    stages = build_stages(qwen=qwen, with_taggers=not args.no_taggers)
    rows, dups = run_pipeline(items, stages)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"[done] wrote {len(rows)} rows to {args.out} (dups={len(dups)})", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
