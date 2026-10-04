"""一次 Agent 调用在 langfuse 里留下什么：trace 只有一个根，工具挂在发起它的那次模型调用下面，根上
记着这次调用交回的结果。

用真的 langfuse SDK，span 导出到内存（见 conftest 的 ``exported_spans``）；模型换成替身，替身照
适配层的样子把每次调用包进 ``generation_span``，所以 generation 走的是真实的那条路。服务端怎么
把这些 span 聚合成 trace 测不到，这里只钉住 SDK 导出了什么。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langfuse._client.attributes import LangfuseOtelSpanAttributes
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import BaseModel

from app.agent.client import ModelClient
from app.agent.core import Agent, AgentConfig
from app.agent.neutral import Message, Role, StreamChunk, ToolCall, ToolDef
from app.agent.tooling import tool
from app.agent.trace import generation_span

AS_ROOT = LangfuseOtelSpanAttributes.AS_ROOT
OUTPUT = LangfuseOtelSpanAttributes.OBSERVATION_OUTPUT
TYPE = LangfuseOtelSpanAttributes.OBSERVATION_TYPE


# ---------------------------------------------------------------------------
# 替身：照适配层的样子调模型
# ---------------------------------------------------------------------------


class _ScriptedModel(ModelClient):
    """按脚本回话的模型。每次调用都像真的适配层那样开一个 ``generation_span``。

    每次调用里先让出一次事件循环，并发的几个调用才会真的交错。
    """

    def __init__(
        self,
        *,
        turns: list[Message] | None = None,
        streams: list[list[StreamChunk]] | None = None,
        structured: list[dict[str, Any]] | None = None,
    ) -> None:
        self._turns = list(turns or [])
        self._streams = list(streams or [])
        self._structured = list(structured or [])

    async def complete(
        self, messages: list[Message], *, tools: list[ToolDef] | None = None, **_: Any
    ) -> Message:
        with generation_span(
            name="scripted", model="scripted", input=[m.to_dict() for m in messages]
        ) as span:
            await asyncio.sleep(0)
            reply = self._turns.pop(0)
            span.update(output=reply.to_dict())
        return reply

    async def stream(
        self, messages: list[Message], *, tools: list[ToolDef] | None = None, **_: Any
    ) -> AsyncIterator[StreamChunk]:
        with generation_span(
            name="scripted", model="scripted", input=[m.to_dict() for m in messages]
        ):
            for chunk in self._streams.pop(0):
                await asyncio.sleep(0)
                yield chunk

    async def structured(
        self, messages: list[Message], *, schema: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        with generation_span(
            name="scripted", model="scripted", input=[m.to_dict() for m in messages]
        ) as span:
            await asyncio.sleep(0)
            data = self._structured.pop(0)
            span.update(output=data)
        return data


def _prompt(name: str, version: int) -> TextPromptClient:
    return TextPromptClient(
        Prompt_Text(
            name=name,
            version=version,
            prompt=f"你是 {name}。",
            config={},
            labels=["production"],
            tags=[],
        )
    )


@pytest.fixture
def wiring(monkeypatch) -> tuple[dict[str, ModelClient], dict[str, TextPromptClient]]:
    """``AgentConfig`` 里的 model_id / prompt_id 各对到哪个替身、哪个 prompt 对象。"""
    models: dict[str, ModelClient] = {}
    prompts: dict[str, TextPromptClient] = {}

    async def _build_model_client(model_id: str) -> ModelClient:
        return models[model_id]

    monkeypatch.setattr("app.agent.core.build_model_client", _build_model_client)
    monkeypatch.setattr(
        "app.agent.core.get_prompt", lambda prompt_id: prompts[prompt_id]
    )
    return models, prompts


def _user(text: str) -> Message:
    return Message(role=Role.USER, content=text)


def _says(text: str) -> Message:
    return Message(role=Role.ASSISTANT, content=text)


def _calls(name: str, call_id: str = "c1") -> Message:
    return Message(
        role=Role.ASSISTANT,
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments={})],
    )


@tool
async def look() -> str:
    """看一眼。"""
    return "看到了"


# ---------------------------------------------------------------------------
# 读导出的 span
# ---------------------------------------------------------------------------


def _finished(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return sorted(exporter.get_finished_spans(), key=lambda s: s.start_time or 0)


def _one(exporter: InMemorySpanExporter, name: str) -> ReadableSpan:
    [span] = [s for s in _finished(exporter) if s.name == name]
    return span


def _generations(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in _finished(exporter) if s.attributes.get(TYPE) == "generation"]


def _roots(exporter: InMemorySpanExporter) -> list[str]:
    """langfuse 服务端会当成 trace 根的 span：没有父节点的，或者被 SDK 标了 as_root 的。"""
    return [
        s.name
        for s in _finished(exporter)
        if s.parent is None or s.attributes.get(AS_ROOT)
    ]


WORLD_ROUND = AgentConfig("world_round", "world-model", "world-round")


# ---------------------------------------------------------------------------
# 工具 span 挂在发起它的那次模型调用下面，而且不是 trace 的根
# ---------------------------------------------------------------------------


async def test_a_tool_span_in_run_hangs_under_the_generation_that_asked_for_it(
    exported_spans, wiring
):
    models, prompts = wiring
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(turns=[_calls("look"), _says("看完了")])

    await Agent(WORLD_ROUND, tools=[look]).run([_user("醒了")])

    root = _one(exported_spans, "world-round")
    asked, _answered = _generations(exported_spans)
    looked = _one(exported_spans, "tool.look")
    assert looked.parent is not None
    assert looked.parent.span_id == asked.context.span_id
    assert looked.context.trace_id == root.context.trace_id
    assert AS_ROOT not in looked.attributes
    assert _roots(exported_spans) == ["world-round"]


async def test_a_tool_span_in_stream_hangs_under_the_generation_that_asked_for_it(
    exported_spans, wiring
):
    models, prompts = wiring
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(
        streams=[
            [
                StreamChunk(text="先看看"),
                StreamChunk(tool_call=ToolCall(id="c1", name="look", arguments={})),
            ],
            [StreamChunk(text="好了")],
        ]
    )

    async for _ in Agent(WORLD_ROUND, tools=[look]).stream([_user("醒了")]):
        pass

    root = _one(exported_spans, "world-round")
    asked, _answered = _generations(exported_spans)
    looked = _one(exported_spans, "tool.look")
    assert looked.parent is not None
    assert looked.parent.span_id == asked.context.span_id
    assert looked.context.trace_id == root.context.trace_id
    assert AS_ROOT not in looked.attributes
    assert _roots(exported_spans) == ["world-round"]


# ---------------------------------------------------------------------------
# 根 span 上记着这次调用交回给调用方的结果
# ---------------------------------------------------------------------------


async def test_run_records_the_message_it_hands_back_on_the_root_span(
    exported_spans, wiring
):
    models, prompts = wiring
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(turns=[_calls("look"), _says("看完了")])

    reply = await Agent(WORLD_ROUND, tools=[look]).run([_user("醒了")])

    root = _one(exported_spans, "world-round")
    assert json.loads(root.attributes[OUTPUT]) == reply.to_dict()


async def test_stream_records_the_whole_text_on_the_root_span(exported_spans, wiring):
    models, prompts = wiring
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(
        streams=[
            [
                StreamChunk(text="先看看"),
                StreamChunk(tool_call=ToolCall(id="c1", name="look", arguments={})),
            ],
            [StreamChunk(text="好"), StreamChunk(text="了")],
        ]
    )

    async for _ in Agent(WORLD_ROUND, tools=[look]).stream([_user("醒了")]):
        pass

    assert _one(exported_spans, "world-round").attributes[OUTPUT] == "先看看好了"


async def test_a_stream_closed_early_records_what_it_had_handed_back_by_then(
    exported_spans, wiring
):
    models, prompts = wiring
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(
        streams=[
            [StreamChunk(text="先"), StreamChunk(text="看看"), StreamChunk(text="再说")]
        ]
    )

    stream = Agent(WORLD_ROUND).stream([_user("醒了")])
    await anext(stream)
    await anext(stream)
    await stream.aclose()

    assert _one(exported_spans, "world-round").attributes[OUTPUT] == "先看看"


class _Verdict(BaseModel):
    ok: bool
    reason: str | None = None


async def test_extract_records_the_model_it_hands_back_on_the_root_span(
    exported_spans, wiring
):
    models, prompts = wiring
    prompts["guard_output_safety"] = _prompt("guard_output_safety", 5)
    models["guard-model"] = _ScriptedModel(structured=[{"ok": False, "reason": "太长"}])

    verdict = await Agent(
        AgentConfig("guard_output_safety", "guard-model", "guard")
    ).extract(_Verdict, messages=[_user("这句话能发吗")])

    root = _one(exported_spans, "guard")
    assert json.loads(root.attributes[OUTPUT]) == verdict.model_dump(mode="json")
