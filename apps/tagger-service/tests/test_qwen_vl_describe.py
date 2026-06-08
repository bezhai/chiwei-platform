from __future__ import annotations

import pytest

from app.qwen_vl_describe import (
    GROUP_A_FIELDS,
    GROUP_B_FIELDS,
    assemble_rows,
    build_image_size_constraint,
    build_row,
    build_tool,
    build_user_text,
    downscale_dims,
    enum_violations,
    filter_unprocessed,
    merge_alloc_conf,
    parse_tool_call,
    resolve_vision_token_cap,
    vision_token_budget,
)


def _wrap(args_json: str) -> str:
    return (
        "<tool_call>\n"
        '{"name": "record_image_overview", "arguments": ' + args_json + "}\n"
        "</tool_call>"
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


def test_parse_tool_call_extracts_arguments() -> None:
    raw = _wrap(
        '{"image_type": "illustration", "main_subject": "a girl", '
        '"num_characters": 1, "viewpoint": "front", "scene_category": "outdoor_nature"}'
    )
    result = parse_tool_call(raw, "a")
    assert result["image_type"] == "illustration"
    assert result["num_characters"] == 1
    assert result["scene_category"] == "outdoor_nature"
    assert "error" not in result


def test_parse_tool_call_strips_whitespace() -> None:
    # 防御：万一模型又在 key/值里塞前后空白，strip 掉
    raw = _wrap('{" image_type ": " illustration ", "num_characters": 2}')
    result = parse_tool_call(raw, "a")
    assert result["image_type"] == "illustration"
    assert result["num_characters"] == 2
    assert "error" not in result


def test_parse_tool_call_missing_field_keeps_none() -> None:
    raw = _wrap('{"image_type": "manga_panel"}')
    result = parse_tool_call(raw, "a")
    assert result["image_type"] == "manga_panel"
    for field in GROUP_A_FIELDS:
        assert field in result
    assert result["viewpoint"] is None
    assert "error" not in result


def test_parse_tool_call_wrong_keys_returns_error() -> None:
    # 期望字段零命中（key 全错）→ 不能静默返回全 null
    raw = _wrap('{"图像类型": "manga_panel"}')
    result = parse_tool_call(raw, "a")
    assert "error" in result
    assert result["raw_output"] == raw


def test_parse_tool_call_malformed_returns_error() -> None:
    raw = "<tool_call>\nthis is not json {oops\n</tool_call>"
    result = parse_tool_call(raw, "a")
    assert "error" in result
    assert result["raw_output"] == raw


def test_parse_tool_call_without_fence() -> None:
    # 没有 <tool_call> 包裹时，直接当裸 JSON 解析也要能 work
    raw = (
        '{"name": "record_image_overview", "arguments": '
        '{"image_type": "product", "num_characters": 0}}'
    )
    result = parse_tool_call(raw, "a")
    assert result["image_type"] == "product"
    assert result["num_characters"] == 0


def test_parse_tool_call_arguments_as_json_string() -> None:
    # 有些模型把 arguments 序列化成 JSON 字符串而非对象，要能再解析一层
    raw = (
        '<tool_call>\n{"name": "record_image_overview", "arguments": '
        '"{\\"image_type\\": \\"product\\", \\"num_characters\\": 0}"}\n</tool_call>'
    )
    result = parse_tool_call(raw, "a")
    assert result["image_type"] == "product"
    assert result["num_characters"] == 0
    assert "error" not in result


def test_parse_tool_call_picks_valid_among_multiple() -> None:
    # 多段 tool_call：第一段坏 / 字段全错，但后面有可用的 → 取可用那段
    raw = (
        "<tool_call>\nbroken {not json\n</tool_call>\n"
        '<tool_call>\n{"name": "record_image_overview", "arguments": '
        '{"image_type": "illustration", "num_characters": 1}}\n</tool_call>'
    )
    result = parse_tool_call(raw, "a")
    assert result["image_type"] == "illustration"
    assert "error" not in result


def test_enum_violations_detects_out_of_enum() -> None:
    # tool calling 软约束不保证 100%，越界值（school_unifrom、indoors）必须被监控出来
    result = {
        "image_type": "indoors",  # 不在 enum
        "main_subject": "anything goes",  # 自由文本无 enum，不算越界
        "num_characters": 3,  # 整数无 enum
        "viewpoint": "front",  # 合法
        "scene_category": None,  # 缺失，不算越界
    }
    violations = enum_violations(result, "a")
    assert violations == ["image_type"]


def test_enum_violations_empty_when_all_valid() -> None:
    result = {
        "image_type": "illustration",
        "main_subject": "x",
        "num_characters": 1,
        "viewpoint": "front",
        "scene_category": "indoor_room",
    }
    assert enum_violations(result, "a") == []


def test_enum_violations_empty_on_error_result() -> None:
    # 解析失败的 result（带 error）不参与越界统计
    assert enum_violations({"error": "boom", "raw_output": "x"}, "a") == []


def test_filter_unprocessed_skips_done_addrs() -> None:
    assets = [
        {"pixiv_addr": "a", "local_path": "/1.jpg"},
        {"pixiv_addr": "b", "local_path": "/2.jpg"},
        {"pixiv_addr": "c", "local_path": "/3.jpg"},
    ]
    remaining = filter_unprocessed(assets, {"a", "c"})
    assert [r["pixiv_addr"] for r in remaining] == ["b"]


def test_filter_unprocessed_empty_done_returns_all() -> None:
    assets = [{"pixiv_addr": "a"}, {"pixiv_addr": "b"}]
    assert filter_unprocessed(assets, set()) == assets


def test_assemble_rows_pairs_groups_in_order() -> None:
    # vLLM batch 输出按顺序回来，assemble 要把每个 asset 的 a/b 两组 raw 正确配对解析
    assets = [
        {"pixiv_addr": "a1", "key": "k1", "local_path": "/1.jpg"},
        {"pixiv_addr": "a2", "key": "k2", "local_path": "/2.jpg"},
    ]
    raws_a = [
        '<tool_call>\n{"name": "x", "arguments": {"image_type": "illustration", "num_characters": 1}}\n</tool_call>',
        '<tool_call>\n{"name": "x", "arguments": {"image_type": "manga_panel", "num_characters": 2}}\n</tool_call>',
    ]
    raws_b = [
        '<tool_call>\n{"name": "x", "arguments": {"exposure_level": "none"}}\n</tool_call>',
        '<tool_call>\n{"name": "x", "arguments": {"exposure_level": "explicit"}}\n</tool_call>',
    ]
    rows = assemble_rows(assets, raws_a, raws_b, "Qwen3-VL-8B-Instruct-FP8")
    assert len(rows) == 2
    assert rows[0]["pixiv_addr"] == "a1"
    assert rows[0]["describe_a"]["image_type"] == "illustration"
    assert rows[0]["describe_b"]["exposure_level"] == "none"
    assert rows[1]["describe_a"]["image_type"] == "manga_panel"
    assert rows[1]["describe_b"]["exposure_level"] == "explicit"
    assert rows[1]["model_version"] == "Qwen3-VL-8B-Instruct-FP8"


def test_assemble_rows_keeps_parse_error() -> None:
    assets = [{"pixiv_addr": "a1", "local_path": "/1.jpg"}]
    raws_a = ["garbage not json"]
    raws_b = ['<tool_call>\n{"name": "x", "arguments": {"mood": "happy"}}\n</tool_call>']
    rows = assemble_rows(assets, raws_a, raws_b, "m")
    assert "error" in rows[0]["describe_a"]
    assert rows[0]["describe_b"]["mood"] == "happy"


def test_jsonl_row_combines_groups() -> None:
    asset = {"pixiv_addr": "addr1", "key": "k1", "local_path": "/p/1.jpg"}
    result_a = {"image_type": "illustration"}
    result_b = {"exposure_level": "none"}
    row = build_row(
        asset,
        result_a,
        result_b,
        elapsed_a=1.5,
        elapsed_b=2.0,
        model_version="Qwen3-VL-8B-Instruct",
    )
    assert row["pixiv_addr"] == "addr1"
    assert row["describe_a"] == result_a
    assert row["describe_b"] == result_b
    assert row["elapsed_s_a"] == 1.5
    assert row["elapsed_s_b"] == 2.0
    assert row["model_version"] == "Qwen3-VL-8B-Instruct"


# 复现 bug：某图 prompt 16598 > max_model_len 16384，vLLM engine core die 拖垮全量。
# 根因：Qwen2VLImageProcessorFast 默认 longest_edge=16777216（≈不限），高分图 vision token 失控。
# 修法：从 max_model_len 反推一个 vision token 预算，再换算成 size.longest_edge 上限。
def test_vision_token_budget_reserves_text_room() -> None:
    # 给 text（tool schema + chat template，实测 ~340）+ 安全 buffer 留出余量，剩下给 vision
    assert vision_token_budget(max_model_len=16384, text_reserve=2048) == 14336


def test_image_size_constraint_maps_vision_tokens_to_pixels() -> None:
    # Qwen2VL fast processor：vision_token = pixels / (patch_size*merge_size)^2 = pixels/1024
    # 所以 longest_edge(=max total pixels) = max_vision_tokens * 1024，把 vision token 钉在上限以内。
    # cap 是独立旋钮：既要 ≤ max_model_len-reserve（防 prompt 超长崩 engine），
    # 又要够小（防大图 batch 把 L4 24G 显存撑爆 OOM）——后者更紧，所以 cap 由它主导。
    size = build_image_size_constraint(max_vision_tokens=8192)
    assert size["shortest_edge"] == 65536
    assert size["longest_edge"] == 8192 * (16 * 2) ** 2  # 8388608
    assert size["longest_edge"] // (16 * 2) ** 2 == 8192


def test_image_size_constraint_smaller_cap_tighter() -> None:
    # cap 越小，longest_edge 越小（图被压得越狠），单调收紧
    small = build_image_size_constraint(max_vision_tokens=4096)
    large = build_image_size_constraint(max_vision_tokens=8192)
    assert small["longest_edge"] < large["longest_edge"]


def test_vision_token_cap_respects_prompt_budget() -> None:
    # 自动取的 cap 不能超过 max_model_len 允许的 prompt 预算（否则又会超长崩 engine）
    cap = resolve_vision_token_cap(requested=12000, max_model_len=16384, text_reserve=2048)
    assert cap == 12000  # 12000 ≤ 14336，放行
    clamped = resolve_vision_token_cap(requested=20000, max_model_len=16384, text_reserve=2048)
    assert clamped == 14336  # 超过 budget，夹到 budget


# 入口处 PIL 预压缩：大图先在 CPU 缩到像素上限再喂 vLLM，GPU 不吃原图大 buffer，
# 减小显存峰值与碎片（OOM 根因：A 组跑完碎片化，B 组分配不出连续显存）。
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


# codex T3 必改2：cap 无下界保护，max_model_len <= text_reserve 会让 cap 变 0/负、max_pixels=0 崩。
def test_vision_token_cap_rejects_nonpositive_budget() -> None:
    with pytest.raises(ValueError):
        resolve_vision_token_cap(requested=8192, max_model_len=2048, text_reserve=2048)


def test_vision_token_cap_rejects_nonpositive_request() -> None:
    with pytest.raises(ValueError):
        resolve_vision_token_cap(requested=0, max_model_len=16384, text_reserve=2048)


# codex T3 建议1：setdefault 在外部已设 PYTORCH_CUDA_ALLOC_CONF 时不会注入 expandable_segments。
def test_merge_alloc_conf_injects_into_empty() -> None:
    assert merge_alloc_conf("") == "expandable_segments:True"


def test_merge_alloc_conf_preserves_existing_other_keys() -> None:
    merged = merge_alloc_conf("max_split_size_mb:128")
    assert "max_split_size_mb:128" in merged
    assert "expandable_segments:True" in merged


def test_merge_alloc_conf_idempotent_when_already_present() -> None:
    # 已有 expandable_segments 配置就不重复追加
    assert merge_alloc_conf("expandable_segments:True") == "expandable_segments:True"
