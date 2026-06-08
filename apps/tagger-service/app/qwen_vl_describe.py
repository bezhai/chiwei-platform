"""Qwen3-VL-8B-Instruct 一图多用：每图调两个工具（function calling），输出 ~10 个结构化语义字段。

裸 JSON prompt 会让模型把 key 翻译成中文 / 改写成同义词复数（gender_distribution→gender_distributions、
mood→emotion），enum 值也飘（school_unifrom）。改用 tool calling 后，function 参数 schema 把 key 和
enum 值都软约束住，遵守度极高。generate 不带 no_repeat_ngram_size——它对结构化输出有害（会往 token
中间塞空格打破 n-gram），只保留 repetition_penalty 防自由文本字段退化。

纯函数（tool schema 构造 / tool_call 解析 / 行合并）本地可测；推理循环依赖 GPU，在 .206 上跑。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

# 字段定义即单一真相源：tool schema 的 properties 和解析时的字段清单都从这里派生。
_GROUP_TOOLS: dict[str, dict[str, Any]] = {
    "a": {
        "name": "record_image_overview",
        "description": "Record the overall type and composition of the image.",
        "properties": {
            "image_type": {
                "type": "string",
                "enum": ["illustration", "manga_panel", "product", "collage", "screenshot", "other"],
            },
            "main_subject": {"type": "string", "description": "short description, under 20 words"},
            "num_characters": {"type": "integer", "description": "count of visible people"},
            "viewpoint": {
                "type": "string",
                "enum": ["front", "back", "side", "overhead", "low_angle", "unknown"],
            },
            "scene_category": {
                "type": "string",
                "enum": [
                    "indoor_room",
                    "classroom",
                    "outdoor_urban",
                    "outdoor_nature",
                    "battle",
                    "abstract",
                    "unknown",
                ],
            },
        },
    },
    "b": {
        "name": "record_character_attributes",
        "description": "Record the human character attributes observed in the image.",
        "properties": {
            "gender_distribution": {
                "type": "string",
                "enum": ["all_female", "all_male", "mixed", "none", "unknown"],
            },
            "age_appearance": {
                "type": "string",
                "enum": ["adult", "young_adult", "teen", "child_or_ambiguous", "unknown"],
            },
            "clothing_type": {
                "type": "string",
                "enum": [
                    "casual",
                    "school_uniform",
                    "swimsuit",
                    "underwear",
                    "nude",
                    "partial",
                    "armor_or_fantasy",
                    "other",
                ],
            },
            "exposure_level": {
                "type": "string",
                "enum": ["none", "mild", "moderate", "significant", "explicit"],
            },
            "mood": {
                "type": "string",
                "enum": ["neutral", "happy", "sad", "angry", "shy", "sensual", "aggressive", "other"],
            },
        },
    },
}

GROUP_A_FIELDS = list(_GROUP_TOOLS["a"]["properties"])
GROUP_B_FIELDS = list(_GROUP_TOOLS["b"]["properties"])

_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)


def _fields_for(group: str) -> list[str]:
    return list(_GROUP_TOOLS[group]["properties"])


def build_tool(group: str) -> dict[str, Any]:
    spec = _GROUP_TOOLS[group]
    properties = spec["properties"]
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
            },
        },
    }


def build_user_text(group: str) -> str:
    name = _GROUP_TOOLS[group]["name"]
    return f"Look at this image and call {name} with the attributes you observe."


# Qwen2VLImageProcessorFast 的 vision token 换算：每个 token 覆盖 (patch_size*merge_size)^2 个像素。
_PATCH_SIZE = 16
_MERGE_SIZE = 2
_PIXELS_PER_VISION_TOKEN = (_PATCH_SIZE * _MERGE_SIZE) ** 2  # 1024
_MIN_PIXELS = 65536  # processor 默认 shortest_edge，保留


def vision_token_budget(max_model_len: int, text_reserve: int) -> int:
    """从 max_model_len 扣掉 text（tool schema + chat template，实测 ~340）和安全 buffer，剩下是 prompt 能塞的 vision token 上限。"""
    return max_model_len - text_reserve


def resolve_vision_token_cap(requested: int, max_model_len: int, text_reserve: int) -> int:
    """确定实际 vision token 上限：取请求值，但不得超过 prompt 预算（否则又会超长崩 engine）。

    两个独立约束：(1) prompt ≤ max_model_len 防 engine core die——上限是 vision_token_budget；
    (2) 大图 batch 别把 L4 24G 显存撑爆 OOM——这个更紧，靠 requested 调小。requested 通常远小于
    budget，这里只是兜底夹紧，防有人把 cap 配得比 prompt 预算还大。
    """
    budget = vision_token_budget(max_model_len, text_reserve)
    if budget <= 0:
        raise ValueError(
            f"max_model_len ({max_model_len}) must exceed text_reserve ({text_reserve})"
        )
    cap = min(requested, budget)
    if cap <= 0:
        raise ValueError(f"vision token cap must be positive, got requested={requested}")
    return cap


def build_image_size_constraint(max_vision_tokens: int) -> dict[str, int]:
    """把 vision token 上限换算成喂给 vLLM mm_processor_kwargs 的 size 约束。

    新版 Qwen2VLImageProcessorFast 不认 max_pixels，只认 size={shortest_edge, longest_edge}，
    且这俩是「总像素」语义不是边长（实测：longest_edge=8388608 → vision token 8132 ≈ 8388608/1024）。
    默认 longest_edge=16777216（≈不限）。vision_token = pixels / (patch_size*merge_size)^2 = pixels/1024，
    所以 longest_edge = max_vision_tokens * 1024，把单图 vision token 钉死在上限内——既防 prompt 超长崩
    engine，也压住大图显存峰值防 OOM。max_vision_tokens 经 resolve_vision_token_cap 夹紧后远大于
    shortest_edge/1024=64，不会出现 longest_edge < shortest_edge 的矛盾。
    """
    return {"shortest_edge": _MIN_PIXELS, "longest_edge": max_vision_tokens * _PIXELS_PER_VISION_TOKEN}


def merge_alloc_conf(prev: str) -> str:
    """把 expandable_segments:True 合进已有的 PYTORCH_CUDA_ALLOC_CONF，保留外部已设的其他键。

    直接 setdefault 会在外部已配（如 max_split_size_mb:128）时整段不注入 expandable_segments、
    碎片修复失效；这里做合并：已含 expandable_segments 则原样返回，否则追加。
    """
    if "expandable_segments" in prev:
        return prev
    return ",".join(filter(None, [prev, "expandable_segments:True"]))


def downscale_dims(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    """保持宽高比把图缩到 ≤ max_pixels；已经够小则原样返回（不放大）。

    在 CPU 入口先压超大图，GPU 上的 vision 预处理就不吃原图大 buffer——降显存峰值、减碎片，
    根治 A 组跑完碎片化导致 B 组 OOM。和 mm_processor_kwargs 的 size 约束同阈值、不会叠加缩放。
    """
    if width * height <= max_pixels:
        return (width, height)
    scale = (max_pixels / (width * height)) ** 0.5
    nw = max(1, int(width * scale))
    nh = max(1, int(height * scale))
    # 极端比例下某一维被 max(1) 兜底，乘积可能仍超 max_pixels（int 截断也会略偏），按另一维反钳一次。
    if nw * nh > max_pixels:
        if nw >= nh:
            nw = max(1, max_pixels // nh)
        else:
            nh = max(1, max_pixels // nw)
    return (nw, nh)


def _extract_fields(segment: str, fields: list[str]) -> dict[str, Any] | None:
    """从一段 JSON 文本里提取期望字段，拿不到就返回 None（让调用方试下一段）。"""
    try:
        parsed = json.loads(segment, strict=False)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    args = parsed.get("arguments", parsed)
    if isinstance(args, str):
        # 有些模型把 arguments 序列化成 JSON 字符串，再解析一层
        try:
            args = json.loads(args, strict=False)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(args, dict):
        return None
    args = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in args.items()}
    if not any(field in args for field in fields):
        return None
    return {field: args.get(field) for field in fields}


def parse_tool_call(raw: str, group: str) -> dict[str, Any]:
    fields = _fields_for(group)
    segments = _TOOL_CALL_RE.findall(raw)
    if not segments:
        segments = [raw.strip()]
    for segment in segments:
        result = _extract_fields(segment, fields)
        if result is not None:
            return result
    return {"error": "no parseable tool call with expected fields", "raw_output": raw}


def enum_violations(result: dict[str, Any], group: str) -> list[str]:
    """返回 result 里取值不在 schema enum 内的字段名（自由文本/整数/None/解析失败不算）。"""
    if "error" in result:
        return []
    properties = _GROUP_TOOLS[group]["properties"]
    violations: list[str] = []
    for field, spec in properties.items():
        enum = spec.get("enum")
        value = result.get(field)
        if enum is not None and value is not None and value not in enum:
            violations.append(field)
    return violations


def filter_unprocessed(
    assets: list[dict[str, Any]], done_addrs: set[str]
) -> list[dict[str, Any]]:
    return [a for a in assets if a.get("pixiv_addr") not in done_addrs]


def build_row(
    asset: dict[str, Any],
    result_a: dict[str, Any],
    result_b: dict[str, Any],
    elapsed_a: float,
    elapsed_b: float,
    model_version: str,
) -> dict[str, Any]:
    return {
        "pixiv_addr": asset.get("pixiv_addr"),
        "key": asset.get("key"),
        "local_path": asset.get("local_path"),
        "describe_a": result_a,
        "describe_b": result_b,
        "elapsed_s_a": elapsed_a,
        "elapsed_s_b": elapsed_b,
        "model_version": model_version,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def assemble_rows(
    assets: list[dict[str, Any]],
    raws_a: list[str],
    raws_b: list[str],
    model_version: str,
) -> list[dict[str, Any]]:
    """把 vLLM batch 的两组 raw 输出按顺序配对解析成行。batch 模式无单条计时，elapsed 置 None。"""
    rows: list[dict[str, Any]] = []
    for asset, raw_a, raw_b in zip(assets, raws_a, raws_b):
        result_a = parse_tool_call(raw_a, "a")
        result_b = parse_tool_call(raw_b, "b")
        rows.append(build_row(asset, result_a, result_b, None, None, model_version))
    return rows


def _build_inputs(
    processor: Any, chunk: list[dict[str, Any]], group: str, max_pixels: int
) -> list[dict[str, Any]]:
    from PIL import Image

    inputs: list[dict[str, Any]] = []
    for asset in chunk:
        path = asset.get("local_path")
        img = Image.open(path).convert("RGB")
        nw, nh = downscale_dims(img.width, img.height, max_pixels)
        if (nw, nh) != (img.width, img.height):
            img = img.resize((nw, nh))
        messages = [{"role": "user", "content": [
            {"type": "image", "image": path},
            {"type": "text", "text": build_user_text(group)},
        ]}]
        text = processor.apply_chat_template(
            messages, tools=[build_tool(group)], tokenize=False, add_generation_prompt=True
        )
        inputs.append({"prompt": text, "multi_modal_data": {"image": img}})
    return inputs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="models/Qwen3-VL-8B-Instruct-FP8")
    parser.add_argument("--assets", type=Path, help="jsonl with pixiv_addr/key/local_path")
    parser.add_argument("--images", nargs="*", help="直接给图片路径（绕过 assets）")
    parser.add_argument("--out", type=Path, default=Path("data/raw/qwen_vl_describe.jsonl"))
    parser.add_argument("--limit", type=int, default=0, help="0 = all")
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--chunk", type=int, default=50,
                        help="每块图数（一次性 decode 进 RAM 的图数，控宿主内存峰值 + 断点粒度）")
    parser.add_argument("--max-model-len", type=int, default=16384)
    parser.add_argument("--max-num-seqs", type=int, default=2,
                        help="并发序列数；大图 prefill 峰值显存高，L4 24G 上 2 较稳")
    parser.add_argument("--gpu-mem-util", type=float, default=0.88)
    parser.add_argument("--text-reserve", type=int, default=2048,
                        help="prompt 里给 text+安全 buffer 预留的 token（夹紧 vision cap 防超长）")
    parser.add_argument("--max-vision-tokens", type=int, default=8192,
                        help="单图 vision token 上限；压住大图防 OOM，8192≈8.4M 像素够清晰")
    parser.add_argument("--swap-space", type=float, default=2.0,
                        help="vLLM CPU swap 空间 GiB；省宿主 RAM（默认 4 太大、和 k3s pod 抢内存）")
    args = parser.parse_args()

    if args.assets:
        assets = read_jsonl(args.assets)
    elif args.images:
        assets = [{"pixiv_addr": p, "key": None, "local_path": p} for p in args.images]
    else:
        print("need --assets or --images", file=sys.stderr)
        return 2
    if args.limit > 0:
        assets = assets[: args.limit]

    # 断点续跑：已写过的 pixiv_addr 跳过，append 模式（中断不丢已产出）
    if args.out.exists():
        done_addrs = {r.get("pixiv_addr") for r in read_jsonl(args.out) if r.get("pixiv_addr") is not None}
        before = len(assets)
        assets = filter_unprocessed(assets, done_addrs)
        print(f"[resume] {len(done_addrs)} done, {before - len(assets)} skipped, {len(assets)} to go", file=sys.stderr)

    # 让分配器用可扩展段，缓解大图 vision 预处理造成的显存碎片（A 组跑完碎片化拖崩 B 组）。
    # 必须在 import torch/vllm 之前设置；合并而非覆盖，保留外部已设的其他 alloc 键。
    os.environ["PYTORCH_CUDA_ALLOC_CONF"] = merge_alloc_conf(os.environ.get("PYTORCH_CUDA_ALLOC_CONF", ""))
    from vllm import LLM, SamplingParams
    from transformers import AutoProcessor

    print(f"[load] {args.model}", file=sys.stderr, flush=True)
    t0 = time.perf_counter()
    processor = AutoProcessor.from_pretrained(args.model)
    vision_cap = resolve_vision_token_cap(args.max_vision_tokens, args.max_model_len, args.text_reserve)
    size_constraint = build_image_size_constraint(vision_cap)
    print(f"[config] vision token cap {vision_cap} -> image size {size_constraint} "
          f"| max_num_seqs={args.max_num_seqs}",
          file=sys.stderr, flush=True)
    llm = LLM(
        model=args.model,
        max_model_len=args.max_model_len,
        limit_mm_per_prompt={"image": 1},
        gpu_memory_utilization=args.gpu_mem_util,
        max_num_seqs=args.max_num_seqs,
        enforce_eager=True,
        swap_space=args.swap_space,
        mm_processor_kwargs={"size": size_constraint},
        # 每张图只喂一次、prompt 全唯一：这两个缓存命中率≈0，纯占 host RAM（mm 缓存默认 4GiB），
        # 是 EngineCore RSS 随张数爬升、最终逼近 OOM killer 的根因。关掉以封住峰值内存。
        mm_processor_cache_gb=0,
        enable_prefix_caching=False,
    )
    sp = SamplingParams(temperature=0.0, max_tokens=args.max_new_tokens, repetition_penalty=1.05)
    model_version = Path(args.model).name
    print(f"[load] done in {time.perf_counter() - t0:.1f}s", file=sys.stderr, flush=True)

    max_pixels = size_constraint["longest_edge"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    parse_errors = 0
    enum_bad = 0
    t_infer = time.perf_counter()
    with args.out.open("a", encoding="utf-8") as handle:
        for start in range(0, len(assets), args.chunk):
            chunk = assets[start : start + args.chunk]
            raws_a = [o.outputs[0].text for o in llm.generate(_build_inputs(processor, chunk, "a", max_pixels), sp)]
            raws_b = [o.outputs[0].text for o in llm.generate(_build_inputs(processor, chunk, "b", max_pixels), sp)]
            rows = assemble_rows(chunk, raws_a, raws_b, model_version)
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                parse_errors += ("error" in row["describe_a"]) + ("error" in row["describe_b"])
                enum_bad += len(enum_violations(row["describe_a"], "a")) + len(enum_violations(row["describe_b"], "b"))
            handle.flush()
            written += len(rows)
            elapsed = time.perf_counter() - t_infer
            print(f"[progress] {written}/{len(assets)} | {elapsed/written:.2f}s/img", file=sys.stderr, flush=True)

    print(
        f"wrote {written} rows | parse_errors={parse_errors}/{written * 2} "
        f"| enum_violations={enum_bad} | out={args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
