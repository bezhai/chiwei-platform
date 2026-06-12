"""Self-written ``@tool`` signature reflection + dispatch.

Replaces langchain ``@tool`` (which fed ``create_agent``). A ``@tool``-wrapped
async function becomes a neutral ``Tool`` exposing:

  - ``.definition`` — a neutral ``ToolDef`` whose ``parameters`` is a JSON
    schema reflected from the function signature,
  - ``.invoke(arguments: dict)`` — calls the wrapped callable.

The reflected schema must be **byte-for-byte equivalent** to what *bare*
langchain ``@tool`` produced on the wire (``convert_to_openai_tool``) — that's
what production fed the model. Bare ``@tool`` (no ``parse_docstring=True``)
means:

  - ``description`` is the **whole docstring verbatim** (Args / Returns included),
    NOT just the summary line;
  - per-parameter ``description`` comes **only** from an explicit
    ``Annotated[..., Field(description=...)]``; the docstring ``Args:`` block is
    NOT split into per-arg descriptions;
  - ``required`` is **omitted entirely** when no params are required.

Changing what the model sees (e.g. lifting Args into per-arg descriptions) is a
behaviour change reserved for a deliberate, separately-verified step — the
langchain cutover must be zero behaviour change here. These tests assert the
bare-@tool contract per parameter type and prove the decorator stacks on the
project's own ``@tool_error``.

The tests deliberately use *synthetic* tool functions, never the real tools.
"""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from pydantic import Field

from app.agent.neutral import ToolCall, ToolResult
from app.agent.tooling import dispatch, tool
from app.agent.tools._common import tool_error

# ---------------------------------------------------------------------------
# Reflection: per-type JSON schema (no docstring Args injection)
# ---------------------------------------------------------------------------


def test_reflect_required_string_param_has_no_args_description():
    @tool
    async def t(query: str) -> str:
        """Search.

        Args:
            query: 搜索关键词。
        """
        return ""

    params = t.definition.parameters
    assert params["type"] == "object"
    # bare @tool does NOT lift the Args: description onto the param.
    assert params["properties"]["query"] == {"type": "string"}
    assert params["required"] == ["query"]


def test_reflect_default_makes_param_optional():
    @tool
    async def t(query: str, gl: str = "CN") -> str:
        """Search.

        Args:
            query: kw.
            gl: region.
        """
        return ""

    params = t.definition.parameters
    assert params["required"] == ["query"]
    assert params["properties"]["gl"]["type"] == "string"
    assert params["properties"]["gl"]["default"] == "CN"
    # no description injected from Args
    assert "description" not in params["properties"]["gl"]


def test_reflect_int_float_bool():
    @tool
    async def t(n: int, ratio: float, flag: bool) -> str:
        """Types."""
        return ""

    props = t.definition.parameters["properties"]
    assert props["n"]["type"] == "integer"
    assert props["ratio"]["type"] == "number"
    assert props["flag"]["type"] == "boolean"
    assert set(t.definition.parameters["required"]) == {"n", "ratio", "flag"}


def test_reflect_list_of_string_no_args_description():
    @tool
    async def t(queries: list[str]) -> dict:
        """Recall.

        Args:
            queries: 自然语言查询列表（批量）
        """
        return {}

    schema = t.definition.parameters["properties"]["queries"]
    assert schema["type"] == "array"
    assert schema["items"] == {"type": "string"}
    assert "description" not in schema


def test_reflect_optional_string_uses_anyof_null():
    @tool
    async def t(note_id: str | None = None) -> dict:
        """Upsert.

        Args:
            note_id: 已有 note id；不传则新建。
        """
        return {}

    schema = t.definition.parameters["properties"]["note_id"]
    # Optional -> anyOf[type, null], matches langchain's convert_to_openai_tool
    assert {"type": "string"} in schema["anyOf"]
    assert {"type": "null"} in schema["anyOf"]
    assert schema["default"] is None
    assert "description" not in schema
    # optional => required key omitted entirely (bare @tool drops empty required)
    assert "required" not in t.definition.parameters


def test_reflect_optional_list_of_string():
    @tool
    async def t(image_list: list[str] | None = None) -> str:
        """Gen.

        Args:
            image_list: 参考图片列表。
        """
        return ""

    schema = t.definition.parameters["properties"]["image_list"]
    assert {"items": {"type": "string"}, "type": "array"} in schema["anyOf"]
    assert {"type": "null"} in schema["anyOf"]


def test_reflect_annotated_field_constraints_and_description():
    @tool
    async def t(
        num: Annotated[int, Field(ge=1, le=10, description="返回结果条数")] = 5,
    ) -> str:
        """Search.

        Args:
            num: 这条 docstring 描述应被忽略（bare @tool 不读 Args）。
        """
        return ""

    schema = t.definition.parameters["properties"]["num"]
    assert schema["type"] == "integer"
    assert schema["minimum"] == 1
    assert schema["maximum"] == 10
    assert schema["default"] == 5
    # explicit Field(description=...) is the ONLY source of a param description
    assert schema["description"] == "返回结果条数"


def test_reflect_annotated_field_description_only():
    @tool
    async def t(
        filenames: Annotated[
            list[str], Field(description='要查看的图片文件名列表，如 ["3.png"]')
        ],
    ) -> str:
        """Read images."""
        return ""

    schema = t.definition.parameters["properties"]["filenames"]
    assert schema["type"] == "array"
    assert schema["items"] == {"type": "string"}
    assert schema["description"] == '要查看的图片文件名列表，如 ["3.png"]'


def test_reflect_no_args_tool_omits_required():
    @tool
    async def t() -> dict:
        """List everything you've got."""
        return {}

    params = t.definition.parameters
    assert params["properties"] == {}
    # bare @tool omits the required key when nothing is required.
    assert "required" not in params


def test_titles_are_stripped_from_schema():
    # langchain's wire schema (convert_to_openai_tool) strips pydantic 'title';
    # our reflector must match so the wire payload is identical.
    @tool
    async def t(query: str, gl: str = "CN") -> str:
        """Search."""
        return ""

    params = t.definition.parameters
    assert "title" not in params
    for prop in params["properties"].values():
        assert "title" not in prop


# ---------------------------------------------------------------------------
# Definition: name + description (whole docstring verbatim)
# ---------------------------------------------------------------------------


def test_definition_name_from_function():
    @tool
    async def recall(queries: list[str]) -> dict:
        """回忆过去。

        Args:
            queries: 列表。
        """
        return {}

    assert recall.definition.name == "recall"


def test_definition_description_is_full_docstring_verbatim():
    @tool
    async def t(query: str) -> str:
        """网页搜索，返回搜索结果及其网页内容。

        Args:
            query: 搜索关键词。
        """
        return ""

    # description is the WHOLE docstring (bare @tool), Args section included.
    assert t.definition.description == (
        "网页搜索，返回搜索结果及其网页内容。\n\nArgs:\n    query: 搜索关键词。"
    )


def test_multiline_docstring_preserved_including_args():
    @tool
    async def t(query: str) -> str:
        """第一行摘要。
        第二行也是摘要。

        Args:
            query: kw.
        """
        return ""

    assert "第一行摘要。" in t.definition.description
    assert "第二行也是摘要。" in t.definition.description
    # whole docstring → Args header IS present (not stripped)
    assert "Args:" in t.definition.description


# ---------------------------------------------------------------------------
# invoke + stacking on @tool_error
# ---------------------------------------------------------------------------


async def test_invoke_calls_wrapped_function():
    @tool
    async def echo(text: str) -> str:
        """Echo."""
        return f"got: {text}"

    out = await echo.invoke({"text": "hi"})
    assert out == "got: hi"


async def test_tool_stacks_above_tool_error_swallows_failure():
    # The whole point: @tool sits ABOVE @tool_error. A failing body returns a
    # ToolOutcomeError dict, it does NOT raise out of invoke.
    @tool
    @tool_error("boom message")
    async def failing(x: str) -> str:
        """Always fails."""
        raise RuntimeError("kaboom")

    result = await failing.invoke({"x": "v"})
    assert isinstance(result, dict)
    assert result["kind"] == "tool_error"
    assert "boom message" in result["message"]
    assert result["detail"]["original_error_type"] == "RuntimeError"


async def test_tool_error_invalid_args_routing_through_stack():
    from app.capabilities._errors import CapabilityInvalidArg

    @tool
    @tool_error("bad")
    async def t(x: str) -> str:
        """Fails with invalid args."""
        raise CapabilityInvalidArg("nope")

    result = await t.invoke({"x": "v"})
    assert result["kind"] == "invalid_args"


async def test_reflection_sees_through_tool_error_wrapper():
    # @tool_error uses functools.wraps, so reflection must still recover the
    # original signature + docstring of the wrapped body.
    @tool
    @tool_error("err")
    async def search_web(
        query: str,
        num: Annotated[int, Field(ge=1, le=10, description="条数")] = 5,
    ) -> str:
        """网页搜索。

        Args:
            query: 搜索关键词。
            num: 这会被 Field 覆盖。
        """
        return ""

    params = search_web.definition.parameters
    # no Args injection; query has no description
    assert params["properties"]["query"] == {"type": "string"}
    assert params["properties"]["num"]["maximum"] == 10
    assert params["properties"]["num"]["description"] == "条数"
    assert params["required"] == ["query"]
    # description = whole docstring verbatim
    assert search_web.definition.description.startswith("网页搜索。")
    assert "Args:" in search_web.definition.description


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


async def test_dispatch_invokes_by_name_returns_tool_result():
    @tool
    async def search_web(query: str) -> str:
        """Search."""
        return f"results for {query}"

    call = ToolCall(id="call_1", name="search_web", arguments={"query": "cats"})
    result = await dispatch([search_web], call)
    assert isinstance(result, ToolResult)
    assert result.tool_call_id == "call_1"
    assert result.content == "results for cats"


async def test_dispatch_unknown_tool_name_returns_not_found_result():
    @tool
    async def only(query: str) -> str:
        """Only."""
        return ""

    call = ToolCall(id="call_x", name="nonexistent", arguments={})
    result = await dispatch([only], call)
    # Surfaces back to the LLM as a tool result, not a raised exception — keeps
    # the agent turn alive, same philosophy as @tool_error.
    assert isinstance(result, ToolResult)
    assert result.tool_call_id == "call_x"
    assert "nonexistent" in str(result.content)


async def test_dispatch_preserves_list_content_blocks():
    # tools returning OpenAI-style image_url blocks (list[dict]) must reach the
    # ToolResult intact — the adapter, not dispatch, normalises them later.
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": "@3.png:"},
        {"type": "image_url", "image_url": {"url": "https://x/3.png"}},
    ]

    @tool
    async def read_images(filenames: list[str]) -> Any:
        """Read."""
        return blocks

    call = ToolCall(id="c1", name="read_images", arguments={"filenames": ["3.png"]})
    result = await dispatch([read_images], call)
    assert result.content == blocks


async def test_dispatch_tool_failure_returns_error_dict_as_content():
    # A tool stacked on @tool_error returns a dict; dispatch carries it as the
    # ToolResult content (json-serialisable) without raising.
    @tool
    @tool_error("oops")
    async def flaky(x: str) -> str:
        """Flaky."""
        raise RuntimeError("down")

    call = ToolCall(id="c2", name="flaky", arguments={"x": "v"})
    result = await dispatch([flaky], call)
    assert isinstance(result, ToolResult)
    assert isinstance(result.content, dict)
    assert result.content["kind"] == "tool_error"


# ---------------------------------------------------------------------------
# dispatch 参数绑定预检（LLM 幻觉参数不再炸整轮 Agent.run）
# ---------------------------------------------------------------------------


async def test_dispatch_unexpected_kwarg_feeds_error_back_not_raise():
    # 2026-06-12 prod 事故：睡前回顾 agent 调 update_day_page 时幻觉串入了
    # update_relationship_page 的 other_user_id 参数，invoke 的 **arguments
    # 直接抛 TypeError 炸掉整轮 Agent.run。绑定失败必须作为 tool result 回喂
    # 模型（同 @tool_error 的 ToolOutcomeError 形态），让模型在同一轮循环里
    # 看到错误、修正参数后重调。
    calls: list[str] = []

    @tool
    async def update_day(narrative: str) -> str:
        """整篇重写昨天页。"""
        calls.append(narrative)
        return "ok"

    call = ToolCall(
        id="c_bind1",
        name="update_day",
        arguments={"narrative": "x", "other_user_id": "u1"},
    )
    result = await dispatch([update_day], call)  # 必须不抛
    assert isinstance(result, ToolResult)
    assert result.tool_call_id == "c_bind1"
    assert isinstance(result.content, dict)
    assert result.content["kind"] == "invalid_args"
    # 错误文本：工具名 + 绑定失败原因（点名多余参数）+ 重调提示
    assert "update_day" in result.content["message"]
    assert "other_user_id" in result.content["message"]
    assert "重新调用" in result.content["message"]
    # 预检挡在调用之前：函数体一次都不能执行
    assert calls == []


async def test_dispatch_missing_required_arg_feeds_error_back_not_raise():
    # 缺必填参数同属绑定失败：回喂模型，不上抛。
    calls: list[str] = []

    @tool
    async def update_day(narrative: str) -> str:
        """整篇重写昨天页。"""
        calls.append(narrative)
        return "ok"

    call = ToolCall(id="c_bind2", name="update_day", arguments={})
    result = await dispatch([update_day], call)
    assert isinstance(result.content, dict)
    assert result.content["kind"] == "invalid_args"
    assert "update_day" in result.content["message"]
    assert "narrative" in result.content["message"]
    assert calls == []


async def test_dispatch_body_typeerror_still_propagates():
    # 铁律：durable 写工具（不包 @tool_error）的失败绝不能被吞。预检只拦
    # "调用前"的参数绑定错误；函数体内部抛出的任何异常——包括 TypeError——
    # 照旧原样上抛，由上游 fail-open / 重试机制处理。
    @tool
    async def durable_write(narrative: str) -> str:
        """Durable write."""
        raise TypeError("body boom")

    call = ToolCall(id="c_bind3", name="durable_write", arguments={"narrative": "x"})
    with pytest.raises(TypeError, match="body boom"):
        await dispatch([durable_write], call)


async def test_dispatch_bind_precheck_sees_through_tool_error_wrapper():
    # @tool_error 的 wrapper 是 (*args, **kwargs) 签名，预检必须穿透
    # functools.wraps 链对"原始签名"做绑定：幻觉参数同样回喂 invalid_args，
    # 而不是漏进 wrapper 内被包成 kind="tool_error"（错误形态要统一）。
    @tool
    @tool_error("wrapped boom")
    async def wrapped(x: str) -> str:
        """Wrapped."""
        return x

    call = ToolCall(id="c_bind4", name="wrapped", arguments={"x": "v", "bogus": 1})
    result = await dispatch([wrapped], call)
    assert isinstance(result.content, dict)
    assert result.content["kind"] == "invalid_args"
    assert "bogus" in result.content["message"]


async def test_dispatch_var_keyword_tool_extra_kwarg_binds_fine():
    # **kwargs 形态的工具：多余参数本来就绑得上，预检不许误伤。
    @tool
    async def flexible(x: str, **extra: Any) -> dict:
        """Flexible."""
        return {"x": x, **extra}

    call = ToolCall(id="c_bind5", name="flexible", arguments={"x": "a", "free": "b"})
    result = await dispatch([flexible], call)
    assert result.content == {"x": "a", "free": "b"}


async def test_dispatch_normal_call_unchanged_by_precheck():
    # 预检通过后行为字节级不变：正常调用的返回值原样进 ToolResult.content。
    @tool
    async def echo2(text: str, n: int = 1) -> str:
        """Echo."""
        return text * n

    call = ToolCall(id="c_bind6", name="echo2", arguments={"text": "hi"})
    result = await dispatch([echo2], call)
    assert result.content == "hi"
