from __future__ import annotations

import pytest

from app.qwen_vl_describe import (
    GROUP_A_FIELDS,
    GROUP_B_FIELDS,
    build_tool,
    build_user_text,
    downscale_dims,
    max_pixels_for_vision_tokens,
    parse_tool_arguments,
)


def test_build_tool_a_has_enum_schema() -> None:
    tool = build_tool("a")
    props = tool["function"]["parameters"]["properties"]
    for field in GROUP_A_FIELDS:
        assert field in props
    # 枚举字段必须带 enum，约束模型只能从候选集里选
    assert "enum" in props["image_type"]
    assert "enum" in props["viewpoint"]
    assert tool["function"]["parameters"]["required"] == GROUP_A_FIELDS


def test_build_tool_b_has_enum_schema() -> None:
    tool = build_tool("b")
    props = tool["function"]["parameters"]["properties"]
    for field in GROUP_B_FIELDS:
        assert field in props
    assert "enum" in props["exposure_level"]
    assert "enum" in props["clothing_type"]


def test_build_user_text_names_the_tool() -> None:
    text = build_user_text("a")
    assert build_tool("a")["function"]["name"] in text


def test_parse_tool_arguments_reads_openai_arguments_string() -> None:
    # OpenAI 兼容接口把参数放在 tool_calls[].function.arguments，是一个 JSON 字符串
    result = parse_tool_arguments(
        '{"image_type": "illustration", "num_characters": 1}', "a"
    )
    assert result["image_type"] == "illustration"
    assert result["num_characters"] == 1
    assert result["viewpoint"] is None
    assert "error" not in result


def test_parse_tool_arguments_broken_json_keeps_raw() -> None:
    result = parse_tool_arguments('{"image_type": "illu', "a")
    assert "error" in result
    assert result["raw_output"] == '{"image_type": "illu'


def test_parse_tool_arguments_wrong_keys_is_error() -> None:
    result = parse_tool_arguments('{"图像类型": "manga_panel"}', "a")
    assert "error" in result


def test_parse_tool_arguments_strips_whitespace_in_keys_and_values() -> None:
    # 防御：万一模型又在 key/值里塞前后空白，strip 掉
    result = parse_tool_arguments('{" image_type ": " illustration ", "num_characters": 2}', "a")
    assert result["image_type"] == "illustration"
    assert result["num_characters"] == 2
    assert "error" not in result


def test_parse_tool_arguments_unwraps_nested_arguments() -> None:
    # 有些模型把整个 tool call 塞进 arguments，或者把 arguments 再序列化成 JSON 字符串
    wrapped = parse_tool_arguments(
        '{"name": "record_image_overview", "arguments": {"image_type": "product", '
        '"num_characters": 0}}',
        "a",
    )
    assert wrapped["image_type"] == "product"
    assert wrapped["num_characters"] == 0

    double_encoded = parse_tool_arguments(
        '{"name": "record_image_overview", "arguments": '
        '"{\\"image_type\\": \\"product\\", \\"num_characters\\": 0}"}',
        "a",
    )
    assert double_encoded["image_type"] == "product"
    assert double_encoded["num_characters"] == 0


def test_parse_tool_arguments_non_object_json_is_error() -> None:
    for arguments in ("[1, 2]", '"just a string"', "null"):
        result = parse_tool_arguments(arguments, "a")
        assert "error" in result, arguments
        assert result["raw_output"] == arguments


def test_max_pixels_for_vision_tokens_scales_by_patch_area() -> None:
    # vision_token = pixels / (patch_size*merge_size)^2 = pixels/1024
    assert max_pixels_for_vision_tokens(4096) == 4096 * 1024
    assert max_pixels_for_vision_tokens(2048) < max_pixels_for_vision_tokens(4096)


def test_max_pixels_for_vision_tokens_rejects_nonpositive() -> None:
    with pytest.raises(ValueError):
        max_pixels_for_vision_tokens(0)


# 入口处 PIL 预压缩：大图先在 CPU 缩到像素上限再发出去，服务端 slot 上下文（-c/-np）吃得下，
# 请求体也不会因为超大 base64 图片膨胀。
def test_downscale_keeps_small_image() -> None:
    # 已经 ≤ 上限的图原样返回，不放大
    assert downscale_dims(100, 100, max_pixels=8388608) == (100, 100)


def test_downscale_shrinks_large_image_under_budget() -> None:
    w, h = downscale_dims(6000, 800, max_pixels=1000000)  # 4.8M -> ≤1M
    assert w * h <= 1000000


def test_downscale_preserves_aspect_ratio() -> None:
    w, h = downscale_dims(4000, 2000, max_pixels=1000000)  # 2:1
    assert abs((w / h) - 2.0) < 0.05


def test_downscale_result_never_exceeds_budget() -> None:
    # codex T3：int() 向下取整必须保证结果 ≤ max_pixels（否则 processor 会再缩一次、白做预压缩）。
    # 含极端比例和恰好踩边界的尺寸。
    for w, h in [(6000, 800), (2480, 3507), (10000, 10000), (1, 9_999_999), (1000, 1000)]:
        nw, nh = downscale_dims(w, h, max_pixels=1_000_000)
        assert nw * nh <= 1_000_000
        assert nw >= 1 and nh >= 1