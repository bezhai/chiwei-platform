"""一次 Agent 调用在 langfuse 里留下什么：trace 只有一个根，工具挂在发起它的那次模型调用下面，根上
记着这次调用交回的结果，每个 generation 关联渲染它的那个 prompt 版本。

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
from app.agent.trace import generation_span, separate_trace

AS_ROOT = LangfuseOtelSpanAttributes.AS_ROOT
OUTPUT = LangfuseOtelSpanAttributes.OBSERVATION_OUTPUT
TYPE = LangfuseOtelSpanAttributes.OBSERVATION_TYPE
PROMPT_NAME = LangfuseOtelSpanAttributes.OBSERVATION_PROMPT_NAME
PROMPT_VERSION = LangfuseOtelSpanAttributes.OBSERVATION_PROMPT_VERSION
TRACE_NAME = LangfuseOtelSpanAttributes.TRACE_NAME


# ---------------------------------------------------------------------------
# 替身：照适配层的样子调模型
# ---------------------------------------------------------------------------


class _Hang:
    """脚本里放它，这次调用就一直等下去，直到被取消。"""


HANG = _Hang()


class _ScriptedModel(ModelClient):
    """按脚本回话的模型。每次调用都像真的适配层那样开一个 ``generation_span``。

    generation 用 ``name`` 命名，测试按它分出是哪个 agent 的模型调用。脚本里放一个异常就在
    这次调用里抛出来，放 ``HANG`` 就一直等到被取消。每次调用里先让出一次事件循环，并发的
    几个调用才会真的交错。
    """

    def __init__(
        self,
        name: str = "scripted",
        *,
        turns: list[Message | BaseException | _Hang] | None = None,
        streams: list[list[StreamChunk]] | None = None,
        structured: list[dict[str, Any]] | None = None,
    ) -> None:
        self._name = name
        self._turns = list(turns or [])
        self._streams = list(streams or [])
        self._structured = list(structured or [])

    def _generation(self, messages: list[Message]):
        return generation_span(
            name=self._name, model=self._name, input=[m.to_dict() for m in messages]
        )

    async def complete(
        self, messages: list[Message], *, tools: list[ToolDef] | None = None, **_: Any
    ) -> Message:
        with self._generation(messages) as span:
            await asyncio.sleep(0)
            reply = self._turns.pop(0)
            if isinstance(reply, _Hang):
                await asyncio.Event().wait()
            if isinstance(reply, BaseException):
                raise reply
            assert isinstance(reply, Message)
            span.update(output=reply.to_dict())
        return reply

    async def stream(
        self, messages: list[Message], *, tools: list[ToolDef] | None = None, **_: Any
    ) -> AsyncIterator[StreamChunk]:
        with self._generation(messages):
            for chunk in self._streams.pop(0):
                await asyncio.sleep(0)
                yield chunk

    async def structured(
        self, messages: list[Message], *, schema: dict[str, Any], **_: Any
    ) -> dict[str, Any]:
        with self._generation(messages) as span:
            await asyncio.sleep(0)
            data = self._structured.pop(0)
            span.update(output=data)
        return data


def _prompt(
    name: str,
    version: int,
    *,
    labels: list[str] | None = None,
    is_fallback: bool = False,
) -> TextPromptClient:
    """一个 langfuse prompt 对象，跟 SDK 取回来的同一个类型；不出网络。"""
    return TextPromptClient(
        Prompt_Text(
            name=name,
            version=version,
            prompt=f"你是 {name}。",
            config={},
            labels=labels or ["production"],
            tags=[],
        ),
        is_fallback=is_fallback,
    )


@pytest.fixture
def models(monkeypatch) -> dict[str, ModelClient]:
    """``AgentConfig.model_id`` 对到哪个替身。"""
    by_id: dict[str, ModelClient] = {}

    async def _build_model_client(model_id: str) -> ModelClient:
        return by_id[model_id]

    monkeypatch.setattr("app.agent.core.build_model_client", _build_model_client)
    return by_id


@pytest.fixture
def prompts(monkeypatch) -> dict[str, TextPromptClient]:
    """``AgentConfig.prompt_id`` 取到哪个 prompt 对象（跳过按泳道取的那一步）。"""
    by_id: dict[str, TextPromptClient] = {}
    monkeypatch.setattr("app.agent.core.get_prompt", lambda prompt_id: by_id[prompt_id])
    return by_id


@pytest.fixture
def labelled(monkeypatch) -> dict[str, TextPromptClient]:
    """Langfuse 上每个 label 指着哪个版本；``get_prompt`` 照它真实的逻辑按泳道 coe-world 取。

    泳道 label 没有的时候 SDK 报错，``get_prompt`` 回落到 production —— 替身照这个样子报错。
    """
    by_label: dict[str, TextPromptClient] = {}

    class _Langfuse:
        def get_prompt(
            self, name: str, *, label: str | None = None, cache_ttl_seconds: int = 0
        ) -> TextPromptClient:
            try:
                return by_label[label or "production"]
            except KeyError:
                raise LookupError(f"{name} has no version labelled {label}") from None

    monkeypatch.setattr("app.agent.prompts._get_client", lambda: _Langfuse())
    monkeypatch.setattr("app.agent.prompts.get_lane", lambda: "coe-world")
    return by_label


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


class _Verdict(BaseModel):
    ok: bool
    reason: str | None = None


WORLD_ROUND = AgentConfig("world_round", "world-model", "world-round")
PERCEPTION = AgentConfig("world_perception", "perception-model", "world-perception")
NPC = AgentConfig("world_npc", "npc-model", "world-npc")
LIFE_MOMENT = AgentConfig("living_life_moment", "life-model", "living-life-moment")
OUTPUT_GUARD = AgentConfig("guard_output_safety", "guard-model", "post-safety-check")


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


def _linked(generation: ReadableSpan) -> tuple[str, int] | None:
    """这个 generation 关联的 prompt（名字, 版本），没关联就是 None。"""
    name = generation.attributes.get(PROMPT_NAME)
    if name is None:
        return None
    return name, generation.attributes[PROMPT_VERSION]


def _links(exporter: InMemorySpanExporter) -> list[tuple[str, tuple[str, int] | None]]:
    """按开始的先后，每次模型调用是谁的（generation 名）、关联了哪个 prompt。"""
    return [(g.name, _linked(g)) for g in _generations(exporter)]


# ---------------------------------------------------------------------------
# 工具 span 挂在发起它的那次模型调用下面，而且不是 trace 的根
# ---------------------------------------------------------------------------


async def test_a_tool_span_in_run_hangs_under_the_generation_that_asked_for_it(
    exported_spans, models, prompts
):
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
    exported_spans, models, prompts
):
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
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(turns=[_calls("look"), _says("看完了")])

    reply = await Agent(WORLD_ROUND, tools=[look]).run([_user("醒了")])

    root = _one(exported_spans, "world-round")
    assert json.loads(root.attributes[OUTPUT]) == reply.to_dict()


async def test_stream_records_the_whole_text_on_the_root_span(
    exported_spans, models, prompts
):
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
    exported_spans, models, prompts
):
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


async def test_extract_records_the_model_it_hands_back_on_the_root_span(
    exported_spans, models, prompts
):
    prompts["guard_output_safety"] = _prompt("guard_output_safety", 5)
    models["guard-model"] = _ScriptedModel(structured=[{"ok": False, "reason": "太长"}])

    verdict = await Agent(OUTPUT_GUARD).extract(
        _Verdict, messages=[_user("这句话能发吗")]
    )

    root = _one(exported_spans, "post-safety-check")
    assert json.loads(root.attributes[OUTPUT]) == verdict.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 每个 generation 关联这次调用实际编译的那个 prompt
# ---------------------------------------------------------------------------


async def test_every_generation_of_a_run_links_the_prompt_it_compiled(
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(turns=[_calls("look"), _says("看完了")])

    await Agent(WORLD_ROUND, tools=[look]).run([_user("醒了")])

    assert _links(exported_spans) == [("scripted", ("world_round", 3))] * 2


async def test_every_generation_of_a_stream_links_the_prompt_it_compiled(
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(
        streams=[
            [StreamChunk(tool_call=ToolCall(id="c1", name="look", arguments={}))],
            [StreamChunk(text="好了")],
        ]
    )

    async for _ in Agent(WORLD_ROUND, tools=[look]).stream([_user("醒了")]):
        pass

    assert _links(exported_spans) == [("scripted", ("world_round", 3))] * 2


async def test_the_generation_of_an_extract_links_the_prompt_it_compiled(
    exported_spans, models, prompts
):
    prompts["guard_output_safety"] = _prompt("guard_output_safety", 5)
    models["guard-model"] = _ScriptedModel(structured=[{"ok": True}])

    await Agent(OUTPUT_GUARD).extract(_Verdict, messages=[_user("这句话能发吗")])

    assert _links(exported_spans) == [("scripted", ("guard_output_safety", 5))]


@pytest.mark.parametrize(
    ("versions_by_label", "linked_version"),
    [({"coe-world": 12, "production": 9}, 12), ({"production": 9}, 9)],
    ids=["lane-label-hit", "falls-back-to-production"],
)
async def test_a_generation_links_the_version_its_lane_resolved_to(
    exported_spans, models, labelled, versions_by_label, linked_version
):
    for label, version in versions_by_label.items():
        labelled[label] = _prompt("world_round", version, labels=[label])
    models["world-model"] = _ScriptedModel(turns=[_says("醒了")])

    await Agent(WORLD_ROUND).run([_user("醒了")])

    assert _links(exported_spans) == [("scripted", ("world_round", linked_version))]


async def test_the_sdk_fallback_prompt_is_not_linked(exported_spans, models, prompts):
    prompts["world_round"] = _prompt("world_round", 1, is_fallback=True)
    models["world-model"] = _ScriptedModel(turns=[_says("醒了")])

    await Agent(WORLD_ROUND).run([_user("醒了")])

    assert _links(exported_spans) == [("scripted", None)]


# ---------------------------------------------------------------------------
# 嵌套：工具里调了另一个 agent，回来之后外层的下一次模型调用仍然关联外层的 prompt
# ---------------------------------------------------------------------------


async def test_a_world_round_links_its_own_prompt_again_after_perception_and_npc(
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    prompts["world_perception"] = _prompt("world_perception", 4)
    prompts["world_npc"] = _prompt("world_npc", 6)
    models["world-model"] = _ScriptedModel(
        "world-model", turns=[_calls("happen"), _says("这一轮完了")]
    )
    models["perception-model"] = _ScriptedModel(
        "perception-model", turns=[_says("她会注意到")]
    )
    models["npc-model"] = _ScriptedModel("npc-model", turns=[_says("老板：早")])

    @tool
    async def happen() -> str:
        """让一件事发生。"""
        with separate_trace():
            noticed = await Agent(PERCEPTION).run([_user("她会注意到吗")])
        with separate_trace():
            said = await Agent(NPC).run([_user("老板说什么")])
        return noticed.text() + said.text()

    await Agent(WORLD_ROUND, tools=[happen]).run([_user("醒了")])

    assert _links(exported_spans) == [
        ("world-model", ("world_round", 3)),
        ("perception-model", ("world_perception", 4)),
        ("npc-model", ("world_npc", 6)),
        ("world-model", ("world_round", 3)),
    ]


async def test_the_output_guard_links_its_own_prompt_and_leaves_the_life_trace_alone(
    exported_spans, models, prompts
):
    """出站安全检查是 life 一轮的 send_message 里限时等的一次 extract，不更新 trace。"""
    prompts["living_life_moment"] = _prompt("living_life_moment", 8)
    prompts["guard_output_safety"] = _prompt("guard_output_safety", 5)
    models["life-model"] = _ScriptedModel(
        "life-model", turns=[_calls("send_message"), _says("说完了")]
    )
    models["guard-model"] = _ScriptedModel("guard-model", structured=[{"ok": True}])

    @tool
    async def send_message() -> str:
        """说一句话。"""
        verdict = await asyncio.wait_for(
            Agent(OUTPUT_GUARD, update_trace=False).extract(
                _Verdict, messages=[], prompt_vars={"response": "早"}
            ),
            timeout=5,
        )
        return "发出去了" if verdict.ok else "拦下了"

    await Agent(LIFE_MOMENT, tools=[send_message]).run([_user("醒了")])

    assert _links(exported_spans) == [
        ("life-model", ("living_life_moment", 8)),
        ("guard-model", ("guard_output_safety", 5)),
        ("life-model", ("living_life_moment", 8)),
    ]
    assert _roots(exported_spans) == ["living-life-moment"]
    assert [
        (s.name, s.attributes[TRACE_NAME])
        for s in _finished(exported_spans)
        if TRACE_NAME in s.attributes
    ] == [("living-life-moment", "living-life-moment")]


async def test_a_call_not_rendered_from_a_prompt_links_none_inside_one_that_is(
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    models["world-model"] = _ScriptedModel(
        "world-model", turns=[_calls("judge"), _says("好")]
    )
    models["judge-model"] = _ScriptedModel("judge-model", structured=[{"ok": True}])

    @tool
    async def judge() -> str:
        """判一下。"""
        verdict = await Agent(
            AgentConfig("", "judge-model", "judge"), update_trace=False
        ).extract(_Verdict, messages=[_user("行吗")])
        return str(verdict.ok)

    await Agent(WORLD_ROUND, tools=[judge]).run([_user("醒了")])

    assert _links(exported_spans) == [
        ("world-model", ("world_round", 3)),
        ("judge-model", None),
        ("world-model", ("world_round", 3)),
    ]


async def test_the_outer_prompt_is_linked_again_after_a_nested_call_fails(
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    prompts["world_npc"] = _prompt("world_npc", 6)
    models["world-model"] = _ScriptedModel(
        "world-model", turns=[_calls("ask_npc"), _says("好")]
    )
    models["npc-model"] = _ScriptedModel("npc-model", turns=[RuntimeError("模型挂了")])

    @tool
    async def ask_npc() -> str:
        """问 NPC。"""
        try:
            await Agent(NPC).run([_user("老板说什么")])
        except RuntimeError:
            return "没问成"
        return "问到了"

    await Agent(WORLD_ROUND, tools=[ask_npc]).run([_user("醒了")])

    assert _links(exported_spans) == [
        ("world-model", ("world_round", 3)),
        ("npc-model", ("world_npc", 6)),
        ("world-model", ("world_round", 3)),
    ]


async def test_the_outer_prompt_is_linked_again_after_a_nested_call_is_cancelled(
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    prompts["world_npc"] = _prompt("world_npc", 6)
    models["world-model"] = _ScriptedModel(
        "world-model", turns=[_calls("ask_npc"), _says("好")]
    )
    models["npc-model"] = _ScriptedModel("npc-model", turns=[HANG])

    @tool
    async def ask_npc() -> str:
        """问 NPC，等不到就算了。"""
        try:
            async with asyncio.timeout(0.05):
                await Agent(NPC).run([_user("老板说什么")])
        except TimeoutError:
            return "没等到"
        return "问到了"

    await Agent(WORLD_ROUND, tools=[ask_npc]).run([_user("醒了")])

    assert _links(exported_spans) == [
        ("world-model", ("world_round", 3)),
        ("npc-model", ("world_npc", 6)),
        ("world-model", ("world_round", 3)),
    ]


async def test_the_outer_prompt_is_linked_again_after_a_nested_stream_is_closed_early(
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    prompts["world_npc"] = _prompt("world_npc", 6)
    models["world-model"] = _ScriptedModel(
        "world-model", turns=[_calls("ask_npc"), _says("好")]
    )
    models["npc-model"] = _ScriptedModel(
        "npc-model", streams=[[StreamChunk(text="老板："), StreamChunk(text="早")]]
    )

    @tool
    async def ask_npc() -> str:
        """问 NPC，听到第一句就够了。"""
        stream = Agent(NPC).stream([_user("老板说什么")])
        first = await anext(stream)
        await stream.aclose()
        return first.text or ""

    await Agent(WORLD_ROUND, tools=[ask_npc]).run([_user("醒了")])

    # 被丢下的那次 NPC 模型调用什么时候收尾由垃圾回收决定，这里只看外层的两次。
    assert [link for name, link in _links(exported_spans) if name == "world-model"] == [
        ("world_round", 3),
        ("world_round", 3),
    ]


async def test_concurrent_calls_each_link_their_own_prompt(
    exported_spans, models, prompts
):
    prompts["world_round"] = _prompt("world_round", 3)
    prompts["living_life_moment"] = _prompt("living_life_moment", 8)
    models["world-model"] = _ScriptedModel(
        "world-model", turns=[_calls("look"), _says("看完了")]
    )
    models["life-model"] = _ScriptedModel(
        "life-model", turns=[_calls("look"), _says("嗯")]
    )

    await asyncio.gather(
        Agent(WORLD_ROUND, tools=[look]).run([_user("醒了")]),
        Agent(LIFE_MOMENT, tools=[look]).run([_user("醒了")]),
    )

    assert sorted(_links(exported_spans)) == [
        ("life-model", ("living_life_moment", 8)),
        ("life-model", ("living_life_moment", 8)),
        ("world-model", ("world_round", 3)),
        ("world-model", ("world_round", 3)),
    ]
