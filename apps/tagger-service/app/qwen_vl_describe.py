"""Qwen3-VL 一图多用的字段契约：tool schema 构造、tool call 解析、图片像素预算。

每图调两个工具（function calling）拿 ~10 个结构化语义字段。裸 JSON prompt 会让模型把 key 翻成中文 /
改写成同义词复数（gender_distribution→gender_distributions、mood→emotion），enum 值也飘
（school_unifrom）。改用 tool calling 后 function 参数 schema 把 key 和 enum 值约束住：走 llama.cpp
的 OpenAI 兼容接口时更强，服务端用语法约束把 enum 卡死在候选集内（实测不带 tools 自由输出会吐出
enum 外的 flat_design）。

本模块只有纯函数，不发请求也不加载模型；HTTP 调用在 app/pipeline/qwen_stage.py。
"""
from __future__ import annotations

import json
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


# Qwen3-VL 的 vision token 换算：每个 token 覆盖 (patch_size*merge_size)^2 个像素。
_PATCH_SIZE = 16
_MERGE_SIZE = 2
_PIXELS_PER_VISION_TOKEN = (_PATCH_SIZE * _MERGE_SIZE) ** 2  # 1024


def max_pixels_for_vision_tokens(max_vision_tokens: int) -> int:
    """单图 vision token 上限换算成像素上限（vision_token = pixels / 1024）。

    上限要留在服务端单 slot 上下文（llama-server 的 -c / -np）之内：超了服务端会直接报
    context 溢出，整张图的三次调用全废；同时也压住 base64 请求体的大小。
    """
    if max_vision_tokens <= 0:
        raise ValueError(f"max_vision_tokens must be positive, got {max_vision_tokens}")
    return max_vision_tokens * _PIXELS_PER_VISION_TOKEN


def downscale_dims(width: int, height: int, max_pixels: int) -> tuple[int, int]:
    """保持宽高比把图缩到 ≤ max_pixels；已经够小则原样返回（不放大）。"""
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


def parse_tool_arguments(arguments: str, group: str) -> dict[str, Any]:
    """解析 OpenAI 兼容响应里 tool_calls[].function.arguments 那个 JSON 字符串。

    正常路径返回该组全部字段（缺的补 None）；解析不出期望字段时返回 error + raw_output，
    下游 merge_row 据此把这一项能力标失败、不污染整行。
    """
    result = _extract_fields(arguments, _fields_for(group))
    if result is None:
        return {
            "error": "tool call arguments are not parseable into expected fields",
            "raw_output": arguments,
        }
    return result
