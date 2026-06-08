from __future__ import annotations

from app.pipeline.merge import merge_row
from app.pipeline.qwen_stage import (
    assemble_describe_result,
    assemble_ocr_result,
    assemble_stage_results,
    build_ocr_prompt,
)

# 合法的裸 JSON tool_call 输出（parse_tool_call 无 <tool_call> 标签时按裸 JSON 解析）
RAW_A = (
    '{"image_type":"illustration","main_subject":"a girl waving",'
    '"num_characters":1,"viewpoint":"front","scene_category":"unknown"}'
)
RAW_B = (
    '{"gender_distribution":"all_female","age_appearance":"teen",'
    '"clothing_type":"casual","exposure_level":"none","mood":"happy"}'
)


def test_build_ocr_prompt_asks_for_text() -> None:
    prompt = build_ocr_prompt()
    assert prompt.strip()
    assert "text" in prompt.lower()


def test_assemble_describe_result_combines_groups() -> None:
    result = assemble_describe_result(RAW_A, RAW_B)
    assert result["describe_a"]["image_type"] == "illustration"
    assert result["describe_a"]["num_characters"] == 1
    assert result["describe_b"]["gender_distribution"] == "all_female"
    assert result["describe_b"]["mood"] == "happy"


def test_assemble_describe_result_keeps_parse_error() -> None:
    result = assemble_describe_result("not json at all", RAW_B)
    assert "error" in result["describe_a"]
    assert result["describe_a"]["raw_output"] == "not json at all"
    assert result["describe_b"]["gender_distribution"] == "all_female"


def test_assemble_ocr_result_dedups_and_counts() -> None:
    # vLLM 退化：同一行刷屏 → clean_ocr_text 去相邻重复
    result = assemble_ocr_result("看板\n看板\n看板\n商店")
    assert result["ocr_text"] == "看板\n商店"
    assert result["ocr_len"] == len("看板\n商店")


def test_assemble_ocr_result_empty_when_no_text() -> None:
    result = assemble_ocr_result("   \n  \n")
    assert result["ocr_text"] == ""
    assert result["ocr_len"] == 0


def test_assemble_stage_results_feeds_merge_row() -> None:
    ids = ["x", "y"]
    results = assemble_stage_results(
        ids,
        raws_a=[RAW_A, "garbage"],
        raws_b=[RAW_B, RAW_B],
        raws_ocr=["hello", ""],
    )
    row_x = merge_row("x", results["x"])
    assert row_x["describe_a"]["image_type"] == "illustration"
    assert row_x["describe_b"]["gender_distribution"] == "all_female"
    assert row_x["ocr"]["ocr_text"] == "hello"
    assert "errors" not in row_x

    # 某图 describe_a 解析失败 → 该能力进 errors、其余能力不受影响
    row_y = merge_row("y", results["y"])
    assert "describe_a" in row_y["errors"]
    assert row_y["describe_b"]["gender_distribution"] == "all_female"
    assert row_y["ocr"]["ocr_text"] == ""
