from __future__ import annotations

import base64
import io
import json
import time
from typing import Any

import httpx
import pytest
from PIL import Image

from app.pipeline.merge import merge_row
from app.pipeline.qwen_stage import (
    QwenVlEndpoint,
    QwenVlHttpStage,
    assemble_ocr_result,
    build_describe_body,
    build_ocr_body,
    build_ocr_prompt,
    encode_image_data_url,
    parse_describe_response,
    parse_ocr_response,
    request_error_result,
)
from app.qwen_vl_describe import build_tool, build_user_text

ARGS_A = json.dumps(
    {
        "image_type": "illustration",
        "main_subject": "a girl waving",
        "num_characters": 1,
        "viewpoint": "overhead",
        "scene_category": "abstract",
    }
)
ARGS_B = json.dumps(
    {
        "gender_distribution": "all_female",
        "age_appearance": "teen",
        "clothing_type": "casual",
        "exposure_level": "none",
        "mood": "happy",
    }
)


def tool_call_payload(arguments: str, *, name: str = "record_image_overview") -> dict[str, Any]:
    """llama-swap 实测形态：finish_reason=tool_calls、content 为空、arguments 是 JSON 字符串。"""
    return {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                },
            }
        ]
    }


def content_payload(
    text: str, *, finish_reason: str = "stop", reasoning: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {"choices": [{"finish_reason": finish_reason, "message": message}]}


def decode_data_url(data_url: str) -> Image.Image:
    header, _, payload = data_url.partition(",")
    assert header == "data:image/jpeg;base64"
    return Image.open(io.BytesIO(base64.b64decode(payload)))


def make_endpoint(**overrides: Any) -> QwenVlEndpoint:
    kwargs: dict[str, Any] = {
        "base_url": "http://llama-swap.invalid:8088/v1",
        "model": "qwen3-vl-8b",
        "concurrency": 1,
    }
    kwargs.update(overrides)
    return QwenVlEndpoint(**kwargs)


def make_stage(handler, **overrides: Any) -> QwenVlHttpStage:
    stage = QwenVlHttpStage(make_endpoint(**overrides), transport=httpx.MockTransport(handler))
    stage.load()
    return stage


def is_describe(body: dict[str, Any]) -> bool:
    return "tools" in body


def describe_group(body: dict[str, Any]) -> str:
    return "a" if body["tools"][0]["function"]["name"] == "record_image_overview" else "b"


# ---------------------------------------------------------------- 纯函数：prompt / 图片编码


def test_build_ocr_prompt_asks_for_text() -> None:
    prompt = build_ocr_prompt()
    assert prompt.strip()
    assert "text" in prompt.lower()


def test_encode_image_data_url_downscales_beyond_pixel_cap() -> None:
    # 服务端 -c 16384 -np 2 → 单 slot 8192 上下文，大图必须在客户端先压住 vision token
    image = Image.new("RGB", (4000, 3000), (10, 20, 30))
    decoded = decode_data_url(encode_image_data_url(image, max_pixels=1_000_000))
    assert decoded.width * decoded.height <= 1_000_000
    assert abs((decoded.width / decoded.height) - (4000 / 3000)) < 0.05


def test_encode_image_data_url_keeps_small_image_and_flattens_alpha() -> None:
    image = Image.new("RGBA", (12, 9), (10, 20, 30, 128))
    decoded = decode_data_url(encode_image_data_url(image, max_pixels=1_000_000))
    assert (decoded.width, decoded.height) == (12, 9)
    assert decoded.mode == "RGB"


# ---------------------------------------------------------------- 纯函数：请求体


def test_build_describe_body_sends_tool_image_and_disables_thinking() -> None:
    body = build_describe_body("qwen3-vl-8b", "data:image/jpeg;base64,AAA", "a", max_new_tokens=256)
    assert body["model"] == "qwen3-vl-8b"
    assert body["tools"] == [build_tool("a")]
    assert body["max_tokens"] == 256
    # thinking 不关会把 token 预算全花在 reasoning_content 上，content/tool_calls 拿空
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    content = body["messages"][0]["content"]
    assert content[0] == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,AAA"},
    }
    assert content[1] == {"type": "text", "text": build_user_text("a")}


def test_build_ocr_body_has_no_tools_and_disables_thinking() -> None:
    body = build_ocr_body("qwen3-vl-8b", "data:image/jpeg;base64,AAA", max_new_tokens=256)
    assert "tools" not in body
    assert body["chat_template_kwargs"]["enable_thinking"] is False
    assert body["messages"][0]["content"][1]["text"] == build_ocr_prompt()


# ---------------------------------------------------------------- 纯函数：响应解析


def test_parse_describe_response_reads_tool_call_arguments() -> None:
    result = parse_describe_response(tool_call_payload(ARGS_A), "a")
    assert result == {
        "image_type": "illustration",
        "main_subject": "a girl waving",
        "num_characters": 1,
        "viewpoint": "overhead",
        "scene_category": "abstract",
    }


def test_parse_describe_response_without_tool_calls_is_error() -> None:
    # 自由文本没有服务端语法约束（实测吐出过 enum 外的 flat_design），解析它等于把脏值
    # 当成功结果送进 merge_row；只有 tool_calls 才算成功。
    content = '{"image_type": "flat_design", "main_subject": "a poster", "num_characters": 2}'
    result = parse_describe_response(content_payload(content), "a")
    assert "error" in result
    assert "image_type" not in result
    assert result["raw_output"] == content


def test_parse_describe_response_bad_arguments_json_keeps_raw() -> None:
    result = parse_describe_response(tool_call_payload('{"image_type": "illu'), "a")
    assert "error" in result
    assert result["raw_output"] == '{"image_type": "illu'


def test_parse_describe_response_empty_content_without_tool_calls_is_error() -> None:
    # thinking 吃光预算：content 空、finish_reason=length，必须暴露成 error 而不是全 null
    payload = content_payload("", finish_reason="length", reasoning="let me look at the image")
    result = parse_describe_response(payload, "a")
    assert "error" in result
    assert "length" in result["error"]
    assert result["raw_output"] == "let me look at the image"


def test_parse_describe_response_without_choices_is_error() -> None:
    result = parse_describe_response({"error": {"message": "model not found"}}, "a")
    assert "error" in result
    assert "model not found" in result["raw_output"]


def test_parse_describe_response_tool_call_without_function_object_is_error() -> None:
    # response.json() 可以回任意合法 JSON：function 是 null 时 .get 会 AttributeError 崩出隔离边界
    payload = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"id": "call_1", "type": "function", "function": None}],
                },
            }
        ]
    }
    result = parse_describe_response(payload, "a")
    assert "error" in result
    assert "image_type" not in result


# 服务端 200 但返回体形状随意（代理页面被当 JSON、错误对象、裸列表/标量）——解析必须回结构化
# error，不能抛异常炸穿单能力隔离。
ARBITRARY_JSON = [
    None,
    3,
    "boom",
    [],
    [{"message": {"content": "x"}}],
    {"choices": "nope"},
    {"choices": [None]},
    {"choices": [{"message": None}]},
    {"choices": [{"message": {"tool_calls": "nope"}}]},
    {"choices": [{"message": {"tool_calls": ["nope"]}}]},
]


@pytest.mark.parametrize("payload", ARBITRARY_JSON)
def test_parse_describe_response_survives_arbitrary_json(payload: Any) -> None:
    result = parse_describe_response(payload, "a")
    assert "error" in result
    assert "raw_output" in result


@pytest.mark.parametrize("payload", ARBITRARY_JSON)
def test_parse_ocr_response_survives_arbitrary_json(payload: Any) -> None:
    result = parse_ocr_response(payload)
    assert "error" in result
    # merge_row 依赖这两个键在所有分支上都在位
    assert result["ocr_text"] == ""
    assert result["ocr_len"] == 0


def test_parse_ocr_response_returns_text_and_len() -> None:
    result = parse_ocr_response(content_payload("看板\n看板\n商店"))
    assert result["ocr_text"] == "看板\n商店"
    assert result["ocr_len"] == len("看板\n商店")
    assert "error" not in result


def test_parse_ocr_response_empty_text_is_not_an_error() -> None:
    # 图里本来就没字：正常停止 + 空正文是合法结果
    result = parse_ocr_response(content_payload("   \n "))
    assert result == {"ocr_text": "", "ocr_len": 0}


def test_parse_ocr_response_truncated_empty_output_is_error() -> None:
    payload = content_payload("", finish_reason="length", reasoning="thinking about the text")
    result = parse_ocr_response(payload)
    assert "error" in result
    assert result["ocr_text"] == ""
    assert result["ocr_len"] == 0
    assert result["raw_output"] == "thinking about the text"


def test_parse_ocr_response_truncated_nonempty_output_keeps_partial_text_and_errors() -> None:
    # 被 length 砍断的转写是不完整的，下游必须知道；已拿到的部分仍然留着便于人工比对
    payload = content_payload("看板\n商店\n营业中", finish_reason="length")
    result = parse_ocr_response(payload)
    assert "error" in result
    assert "length" in result["error"]
    assert result["ocr_text"] == "看板\n商店\n营业中"
    assert result["ocr_len"] == len("看板\n商店\n营业中")


def test_parse_ocr_response_unexpected_finish_reason_is_error() -> None:
    # 只认 stop；其他停止原因（content_filter / 缺字段）都不是完整转写
    for finish_reason in ("content_filter", None):
        result = parse_ocr_response(content_payload("看板", finish_reason=finish_reason))
        assert "error" in result, finish_reason
        assert result["ocr_text"] == "看板"
        assert result["ocr_len"] == len("看板")


def test_parse_ocr_response_missing_or_non_string_content_is_error() -> None:
    for message in ({"role": "assistant"}, {"role": "assistant", "content": ["看板"]}):
        payload = {"choices": [{"finish_reason": "stop", "message": message}]}
        result = parse_ocr_response(payload)
        assert "error" in result, message
        assert result["ocr_text"] == ""
        assert result["ocr_len"] == 0


def test_assemble_ocr_result_dedups_and_counts() -> None:
    result = assemble_ocr_result("看板\n看板\n看板\n商店")
    assert result["ocr_text"] == "看板\n商店"
    assert result["ocr_len"] == len("看板\n商店")


def test_request_error_result_keeps_response_body() -> None:
    request = httpx.Request("POST", "http://x/v1/chat/completions")
    response = httpx.Response(500, text="upstream exploded", request=request)
    exc = httpx.HTTPStatusError("boom", request=request, response=response)
    result = request_error_result(exc)
    assert result["error"].startswith("HTTPStatusError:")
    assert result["raw_output"] == "upstream exploded"


def test_request_error_result_without_response() -> None:
    result = request_error_result(httpx.ConnectError("connection refused"))
    assert result["error"] == "ConnectError: connection refused"
    assert result["raw_output"] == ""


# ---------------------------------------------------------------- 端点契约


def test_endpoint_rejects_missing_base_url_or_model() -> None:
    with pytest.raises(ValueError):
        QwenVlEndpoint(base_url="", model="qwen3-vl-8b")
    with pytest.raises(ValueError):
        QwenVlEndpoint(base_url="http://x/v1", model="")


def test_endpoint_builds_chat_completions_url() -> None:
    endpoint = QwenVlEndpoint(base_url="http://x:8088/v1/", model="m")
    assert endpoint.chat_completions_url == "http://x:8088/v1/chat/completions"


# ---------------------------------------------------------------- 阶段：HTTP 替身


def test_stage_run_produces_three_capabilities_per_image() -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        assert request.url == httpx.URL("http://llama-swap.invalid:8088/v1/chat/completions")
        if is_describe(body):
            group = describe_group(body)
            return httpx.Response(200, json=tool_call_payload(ARGS_A if group == "a" else ARGS_B))
        return httpx.Response(200, json=content_payload("hello world"))

    stage = make_stage(handle)
    out = stage.run([("x", Image.new("RGB", (8, 8)))])
    stage.unload()

    assert out["x"]["describe_a"]["image_type"] == "illustration"
    assert out["x"]["describe_b"]["gender_distribution"] == "all_female"
    assert out["x"]["ocr"] == {"ocr_text": "hello world", "ocr_len": 11}
    assert len(seen) == 3
    assert [is_describe(body) for body in seen] == [True, True, False]
    assert {describe_group(body) for body in seen if is_describe(body)} == {"a", "b"}
    assert all(body["model"] == "qwen3-vl-8b" for body in seen)


def test_stage_run_feeds_merge_row() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if is_describe(body):
            group = describe_group(body)
            return httpx.Response(200, json=tool_call_payload(ARGS_A if group == "a" else ARGS_B))
        return httpx.Response(200, json=content_payload(""))

    stage = make_stage(handle)
    out = stage.run([("x", Image.new("RGB", (8, 8)))])
    stage.unload()

    row = merge_row("x", out["x"])
    assert row["describe_a"]["image_type"] == "illustration"
    assert row["describe_b"]["mood"] == "happy"
    assert row["ocr"] == {"ocr_text": "", "ocr_len": 0}
    assert "errors" not in row


def test_stage_sends_bearer_token_only_when_configured() -> None:
    headers: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("authorization"))
        body = json.loads(request.content)
        if is_describe(body):
            return httpx.Response(200, json=tool_call_payload(ARGS_A))
        return httpx.Response(200, json=content_payload(""))

    stage = make_stage(handle)
    stage.run([("x", Image.new("RGB", (8, 8)))])
    stage.unload()
    assert headers == [None, None, None]

    headers.clear()
    stage = make_stage(handle, api_key="secret")
    stage.run([("x", Image.new("RGB", (8, 8)))])
    stage.unload()
    assert headers == ["Bearer secret"] * 3


def test_stage_isolates_http_500_to_the_failing_capability() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if is_describe(body):
            group = describe_group(body)
            return httpx.Response(200, json=tool_call_payload(ARGS_A if group == "a" else ARGS_B))
        return httpx.Response(500, text="slot unavailable")

    stage = make_stage(handle)
    out = stage.run([("x", Image.new("RGB", (8, 8)))])
    stage.unload()

    assert out["x"]["describe_a"]["image_type"] == "illustration"
    assert "error" in out["x"]["ocr"]
    assert out["x"]["ocr"]["ocr_text"] == ""
    assert out["x"]["ocr"]["ocr_len"] == 0
    assert out["x"]["ocr"]["raw_output"] == "slot unavailable"
    row = merge_row("x", out["x"])
    assert set(row["errors"]) == {"ocr"}


def test_stage_isolates_timeout_to_one_image_and_finishes_batch() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data_url = body["messages"][0]["content"][0]["image_url"]["url"]
        second_image = decode_data_url(data_url).width == 9
        if is_describe(body):
            group = describe_group(body)
            if group == "b" and second_image:
                raise httpx.ReadTimeout("read timed out", request=request)
            return httpx.Response(200, json=tool_call_payload(ARGS_A if group == "a" else ARGS_B))
        return httpx.Response(200, json=content_payload("text"))

    stage = make_stage(handle)
    out = stage.run([("x", Image.new("RGB", (8, 8))), ("y", Image.new("RGB", (9, 8)))])
    stage.unload()

    assert out["x"]["describe_b"]["mood"] == "happy"
    assert "error" in out["y"]["describe_b"]
    assert out["y"]["describe_b"]["error"].startswith("ReadTimeout:")
    assert out["y"]["describe_a"]["image_type"] == "illustration"
    assert out["y"]["ocr"]["ocr_text"] == "text"


def test_stage_reports_non_json_response_as_capability_error() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>proxy error</html>")

    stage = make_stage(handle)
    out = stage.run([("x", Image.new("RGB", (8, 8)))])
    stage.unload()

    assert "error" in out["x"]["describe_a"]
    assert "error" in out["x"]["ocr"]


def test_stage_isolates_unexpected_json_shape_and_finishes_the_batch() -> None:
    # 服务端 200 回了一个裸列表：解析必须只让这一项能力失败，整批其余结果照常产出
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data_url = body["messages"][0]["content"][0]["image_url"]["url"]
        second_image = decode_data_url(data_url).width == 9
        if is_describe(body):
            group = describe_group(body)
            return httpx.Response(200, json=tool_call_payload(ARGS_A if group == "a" else ARGS_B))
        if second_image:
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=content_payload("text"))

    stage = make_stage(handle)
    out = stage.run([("x", Image.new("RGB", (8, 8))), ("y", Image.new("RGB", (9, 8)))])
    stage.unload()

    assert out["x"]["ocr"] == {"ocr_text": "text", "ocr_len": 4}
    assert "error" in out["y"]["ocr"]
    assert out["y"]["ocr"]["ocr_text"] == ""
    assert out["y"]["ocr"]["ocr_len"] == 0
    assert out["y"]["describe_a"]["image_type"] == "illustration"
    assert set(merge_row("y", out["y"])["errors"]) == {"ocr"}


def test_stage_isolates_a_parser_crash_to_the_failing_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 解析必须在异常隔离之内：解析器炸了也只能标这一项能力，不能让整批 run() 抛异常
    def boom(payload: Any) -> dict[str, Any]:
        raise RuntimeError("unexpected response shape")

    monkeypatch.setattr("app.pipeline.qwen_stage.parse_ocr_response", boom)

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if is_describe(body):
            group = describe_group(body)
            return httpx.Response(200, json=tool_call_payload(ARGS_A if group == "a" else ARGS_B))
        return httpx.Response(200, json=content_payload("text"))

    stage = make_stage(handle)
    out = stage.run([("x", Image.new("RGB", (8, 8)))])
    stage.unload()

    assert out["x"]["describe_a"]["image_type"] == "illustration"
    assert out["x"]["ocr"]["error"] == "RuntimeError: unexpected response shape"
    assert out["x"]["ocr"]["ocr_text"] == ""
    assert out["x"]["ocr"]["ocr_len"] == 0


def test_stage_isolates_a_describe_parser_crash_to_the_failing_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(payload: Any, group: str) -> dict[str, Any]:
        raise RuntimeError("unexpected response shape")

    monkeypatch.setattr("app.pipeline.qwen_stage.parse_describe_response", boom)

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if is_describe(body):
            return httpx.Response(200, json=tool_call_payload(ARGS_A))
        return httpx.Response(200, json=content_payload("text"))

    stage = make_stage(handle)
    out = stage.run([("x", Image.new("RGB", (8, 8)))])
    stage.unload()

    assert out["x"]["describe_a"]["error"] == "RuntimeError: unexpected response shape"
    assert out["x"]["describe_b"]["error"] == "RuntimeError: unexpected response shape"
    assert out["x"]["ocr"] == {"ocr_text": "text", "ocr_len": 4}


def test_stage_keeps_id_to_result_mapping_under_concurrency() -> None:
    # 并发下结果必须按 id 归位：先发的图故意最慢返回，错序完成也不能串行错配
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data_url = body["messages"][0]["content"][0]["image_url"]["url"]
        index = decode_data_url(data_url).width - 10
        time.sleep((5 - index) * 0.01)
        if is_describe(body):
            args = json.loads(ARGS_A)
            args["main_subject"] = f"image {index}"
            return httpx.Response(200, json=tool_call_payload(json.dumps(args)))
        return httpx.Response(200, json=content_payload(f"ocr {index}"))

    items = [(f"id{i}", Image.new("RGB", (10 + i, 8))) for i in range(5)]
    stage = make_stage(handle, concurrency=4)
    out = stage.run(items)
    stage.unload()

    for i in range(5):
        assert out[f"id{i}"]["describe_a"]["main_subject"] == f"image {i}"
        assert out[f"id{i}"]["ocr"]["ocr_text"] == f"ocr {i}"


def test_stage_marks_all_capabilities_when_image_cannot_be_encoded() -> None:
    class BrokenImage:
        def convert(self, mode: str):  # noqa: ANN202 - 测试替身
            raise OSError("truncated file")

    def handle(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不应被调用
        raise AssertionError("no request should be sent for an unencodable image")

    stage = make_stage(handle)
    out = stage.run([("x", BrokenImage())])
    stage.unload()

    assert "error" in out["x"]["describe_a"]
    assert "error" in out["x"]["describe_b"]
    assert "error" in out["x"]["ocr"]
    assert out["x"]["ocr"]["ocr_len"] == 0


def test_stage_run_before_load_fails_loudly() -> None:
    stage = QwenVlHttpStage(make_endpoint())
    with pytest.raises(RuntimeError):
        stage.run([("x", Image.new("RGB", (8, 8)))])


def test_stage_unload_closes_the_http_client_and_is_idempotent() -> None:
    def handle(request: httpx.Request) -> httpx.Response:  # pragma: no cover - 不发请求
        raise AssertionError("unused")

    stage = QwenVlHttpStage(make_endpoint(), transport=httpx.MockTransport(handle))
    stage.load()
    client = stage.client
    assert client is not None and not client.is_closed
    stage.unload()
    stage.unload()
    assert client.is_closed
    assert stage.client is None
