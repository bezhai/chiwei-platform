"""Self-written ``@tool`` decorator + signature reflection + dispatch.

Replaces langchain's ``@tool`` (which built the ``StructuredTool`` fed to
``create_agent``). A decorated async function becomes a neutral :class:`Tool`:

  - ``.definition`` — a neutral ``ToolDef`` (name + description + JSON-schema
    ``parameters``), reflected from the function's signature and docstring,
  - ``.invoke(arguments)`` — calls the wrapped coroutine with ``**arguments``.

The reflected schema is **byte-for-byte equivalent** to what *bare* langchain
``@tool`` emitted on the wire via ``convert_to_openai_tool`` — that's exactly
what production fed the model. Bare ``@tool`` (no ``parse_docstring=True``)
means:

  - ``description`` is the **whole docstring verbatim** (Args / Returns
    sections included), NOT just the summary line,
  - per-parameter ``description`` comes **only** from an explicit
    ``Annotated[..., Field(description=...)]``; the docstring ``Args:`` block is
    NOT split into per-arg descriptions,
  - ``required`` is **omitted entirely** when no parameter is required,
  - pydantic ``title`` keys are stripped.

Changing what the model sees (e.g. lifting Args into per-arg descriptions) is a
behaviour change reserved for a deliberate, separately-verified step — the
langchain cutover must be zero behaviour change to the tool contract.

Equivalence is structural, not coincidental: reflection builds the param schema
with the *same* ``pydantic.create_model`` machinery langchain uses, so per-type
output (int constraints, ``Optional`` -> ``anyOf[type, null]``, ``list[str]``
-> array of string) matches by construction.

``@tool`` stacks **above** the project's ``@tool_error`` (``_common.py``): the
wrapped callable already returns a ``ToolOutcomeError`` dict on failure instead
of raising, and reflection sees through ``functools.wraps`` to the original
signature/docstring.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import create_model

from app.agent.neutral import ToolCall, ToolDef, ToolResult

logger = logging.getLogger(__name__)


class Tool:
    """A neutral tool: reflected ``ToolDef`` + async invoke over a callable."""

    def __init__(self, func: Callable[..., Awaitable[Any]]) -> None:
        if not inspect.iscoroutinefunction(_unwrap(func)):
            raise TypeError(f"@tool expects an async function, got {func!r}")
        self._func = func
        self.name: str = _unwrap(func).__name__
        self.definition: ToolDef = _build_definition(func, self.name)

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        """Call the wrapped coroutine with ``**arguments`` and return its value.

        No error handling here: failures are the wrapped ``@tool_error``'s job
        (it returns a structured dict instead of raising). A tool *not* wrapped
        in ``@tool_error`` will propagate, by design.
        """
        return await self._func(**arguments)


def tool(func: Callable[..., Awaitable[Any]]) -> Tool:
    """Decorator: wrap an async function into a neutral :class:`Tool`."""
    return Tool(func)


# ---------------------------------------------------------------------------
# Reflection
# ---------------------------------------------------------------------------


def _unwrap(func: Callable[..., Any]) -> Callable[..., Any]:
    """Follow ``functools.wraps`` chains (e.g. ``@tool_error``) to the body.

    ``inspect.signature`` / ``getdoc`` already follow ``__wrapped__``, but we
    need the underlying coroutine for the async check and the raw ``__name__``.
    """
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


def _build_definition(func: Callable[..., Any], name: str) -> ToolDef:
    # Bare langchain @tool uses the WHOLE docstring as the description — no
    # summary/Args split. ``inspect.getdoc`` cleans indentation the same way
    # langchain does (it reads ``func.__doc__`` via the same cleaning).
    description = inspect.getdoc(func) or ""
    parameters = build_parameters_schema(func)
    return ToolDef(name=name, description=description, parameters=parameters)


def build_parameters_schema(func: Callable[..., Any]) -> dict[str, Any]:
    """Reflect a JSON-schema ``parameters`` object from ``func``'s signature.

    Built via ``pydantic.create_model`` (langchain's own path), then matched to
    bare ``convert_to_openai_tool`` wire shape:
      - ``title`` keys stripped,
      - per-param description only from explicit ``Field(description=...)``
        (no docstring ``Args:`` injection),
      - ``required`` omitted entirely when empty.
    """
    # ``eval_str=True`` resolves string annotations (tools use
    # ``from __future__ import annotations``) into real types/``Annotated`` so
    # ``create_model`` can build the schema instead of choking on str forms.
    sig = inspect.signature(func, eval_str=True)

    fields: dict[str, Any] = {}
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        annotation = (
            param.annotation if param.annotation is not inspect.Parameter.empty else Any
        )
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[pname] = (annotation, default)

    model = create_model(func.__name__, **fields)
    schema = model.model_json_schema()

    properties: dict[str, Any] = schema.get("properties", {})
    _strip_titles(schema)

    out: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    # Bare langchain omits ``required`` entirely when no field is required.
    required = schema.get("required", [])
    if required:
        out["required"] = required
    return out


def _strip_titles(schema: dict[str, Any]) -> None:
    """Recursively drop ``title`` keys (pydantic adds them; the wire omits)."""
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        _strip_titles_node(prop)


def _strip_titles_node(node: Any) -> None:
    if isinstance(node, dict):
        node.pop("title", None)
        for value in node.values():
            _strip_titles_node(value)
    elif isinstance(node, list):
        for item in node:
            _strip_titles_node(item)


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


async def dispatch(tools: list[Tool], call: ToolCall) -> ToolResult:
    """Find the tool named ``call.name``, invoke it, wrap its return.

    The tool's raw return value (``str`` / ``dict`` / list of content blocks) is
    carried on ``ToolResult.content`` untouched; the adapter serialises it to
    the provider wire later (cutover). An unknown tool name surfaces as a
    ``ToolResult`` (not a raised exception) so the agent turn stays alive — same
    philosophy as ``@tool_error``: a single bad tool call shouldn't kill the
    whole reply.

    调用前先做**参数绑定预检**（``_check_binding``）：LLM 幻觉出的参数错误
    （多参数 / 缺必填 / 参数名错）同样以 ``ToolResult`` 回喂模型修正重调，
    不向上抛。预检通过后照常调用——函数体内部抛出的任何异常仍原样传播
    （durable 写工具不包 @tool_error 的设计语义不受影响）。
    """
    by_name = {t.name: t for t in tools}
    target = by_name.get(call.name)
    if target is None:
        known = ", ".join(sorted(by_name)) or "(none)"
        return ToolResult(
            tool_call_id=call.id,
            content=f"unknown tool {call.name!r}; available: {known}",
        )
    binding_outcome = _check_binding(target, call)
    if binding_outcome is not None:
        return ToolResult(tool_call_id=call.id, content=binding_outcome)
    result = await target.invoke(call.arguments)
    return ToolResult(tool_call_id=call.id, content=result)


def _check_binding(target: Tool, call: ToolCall) -> dict[str, Any] | None:
    """绑定预检：把 LLM 幻觉出的参数错误拦在工具函数体之外。

    2026-06-12 prod 事故：睡前回顾 agent 调 update_day_page 时幻觉串入了
    update_relationship_page 的 ``other_user_id`` 参数，``invoke`` 的
    ``**arguments`` 直接抛 TypeError 炸掉整轮 Agent.run。绑定失败属于模型
    的调用错误，应当像 ``@tool_error`` 一样以 ``ToolOutcomeError`` 形态回喂
    模型，让它在同一轮循环里看到错误、修正参数后重调。

    ``inspect.signature`` 会穿透 ``functools.wraps`` 链（如 @tool_error 的
    wrapper）拿到原始签名，对 ``functools.partial`` 也原生支持；**kwargs
    形态的工具天然绑得上多余参数，不会误伤。只有"绑定本身"抛 TypeError
    才回喂——函数体内部的异常与本函数无关，照旧传播。

    绑定失败返回 outcome dict，可绑定返回 ``None``。
    """
    try:
        inspect.signature(target._func).bind(**call.arguments)
    except TypeError as exc:
        # 局部 import 断循环：app.agent.tools 包 __init__ 会拉起各工具模块，
        # 而工具模块都 ``from app.agent.tooling import tool``。
        from app.agent.tools.outcome import ToolOutcomeError

        logger.warning(
            "dispatch: %s 参数绑定失败 — %s (arguments=%s)",
            call.name,
            exc,
            sorted(call.arguments),
        )
        return ToolOutcomeError(
            kind="invalid_args",
            message=(
                f"工具 {call.name} 参数绑定失败：{exc}。"
                "请检查该工具的参数定义，修正参数后重新调用。"
            ),
            detail={"original_error_type": type(exc).__name__},
        ).model_dump()
    return None
