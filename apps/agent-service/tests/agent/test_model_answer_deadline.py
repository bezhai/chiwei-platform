"""一次模型调用等对方回话有期限：对方一直不开口，这次调用到期限就失败，trace 上记成错误。

起因（2026-10-05，coe-world）：life-model（gemini-3.7-flash，经内部网关）的调用卡在 TLS 握手上，
google-genai SDK 不设任何超时，能把它结束的只有 900 秒的 moment 占用——她一卡就是 15 分钟，
Langfuse 里那次 generation 是 DEFAULT、没有输出，看不出出了事。

模型 SDK 换成替身，替身的调用可以一直等到被取消；``generation_span`` 和 Agent 的根 span 是真的，
span 导出到内存（conftest 的 ``exported_spans``）。期限在测试里压到几十毫秒；每个调用外面再套一层
守卫，期限没生效时测试报失败，而不是挂住。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from langfuse._client.attributes import LangfuseOtelSpanAttributes
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from app.agent.adapters.gemini import GeminiAdapter
from app.agent.adapters.openai import OpenAIAdapter
from app.agent.client import ModelClient
from app.agent.contracts import AgentSpec, Segment
from app.agent.core import Agent
from app.agent.neutral import Message, Role
from app.agent.prompts import PromptSet
from app.agent.round import open_call
from app.capabilities._errors import CapabilityTimeout

LEVEL = LangfuseOtelSpanAttributes.OBSERVATION_LEVEL
STATUS = LangfuseOtelSpanAttributes.OBSERVATION_STATUS_MESSAGE
TYPE = LangfuseOtelSpanAttributes.OBSERVATION_TYPE

# 测试里的期限，和等它的守卫。守卫比期限长得多：守卫先到点，说明期限没有生效。
DEADLINE = 0.1
GUARD = 2.0

LIFE_MOMENT = AgentSpec(
    segments=("living_life_moment",), model="life-model", trace_name="living-life-moment"
)


def _her_call(spec: AgentSpec = LIFE_MOMENT):
    return open_call("living_life_moment", spec, lane="coe-living")


async def _her_prompts(spec: AgentSpec = LIFE_MOMENT) -> PromptSet:
    return await PromptSet.resolve(
        {"living_life_moment": spec},
        {"living_life_moment": Segment("living_life_moment", "living_life_moment")},
        None,
    )
SCHEMA = {"title": "verdict", "type": "object", "properties": {}}


@pytest.fixture(autouse=True)
def _short_deadline(monkeypatch):
    monkeypatch.setattr("app.agent.client.MODEL_ANSWER_SECONDS", DEADLINE)


async def _guarded(call: Awaitable[Any]) -> Any:
    try:
        async with asyncio.timeout(GUARD) as guard:
            return await call
    except TimeoutError:
        if guard.expired():
            pytest.fail(f"这次调用过了 {GUARD} 秒还在等：没有东西限制它等多久")
        raise


async def _forever() -> None:
    await asyncio.Event().wait()


def _user(text: str) -> Message:
    return Message(role=Role.USER, content=text)


# ---------------------------------------------------------------------------
# 模型 SDK 的替身
# ---------------------------------------------------------------------------


def _gemini(
    monkeypatch, *, answer: Callable[[], Awaitable[Any]] = _forever
) -> list[dict[str, Any]]:
    """把 GeminiAdapter 用的 genai.Client 换成替身，返回它收到的每一次请求。

    ``answer`` 是请求怎么回（默认一直不回）。
    """
    requests: list[dict[str, Any]] = []

    async def generate_content(**kw: Any) -> Any:
        requests.append(kw)
        return await answer()

    models = SimpleNamespace(generate_content=generate_content)
    client = SimpleNamespace(aio=SimpleNamespace(models=models))
    monkeypatch.setattr(
        "app.agent.adapters.gemini.genai.Client", lambda **_kwargs: client
    )
    return requests


def _openai_never_answers(monkeypatch) -> None:
    async def create(**_kw: Any) -> Any:
        await _forever()

    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(
        "app.agent.adapters.openai.AsyncOpenAI", lambda **_kwargs: client
    )


def _gemini_adapter() -> GeminiAdapter:
    return GeminiAdapter(
        model_name="gemini-3.7-flash", api_key="k", base_url="https://gateway"
    )


def _openai_adapter() -> OpenAIAdapter:
    return OpenAIAdapter(
        model_name="gpt-5.5", api_key="sk", base_url="https://x", client_type="openai"
    )


# ---------------------------------------------------------------------------
# 读导出的 span
# ---------------------------------------------------------------------------


def _finished(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return sorted(exporter.get_finished_spans(), key=lambda s: s.start_time or 0)


def _generations(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in _finished(exporter) if s.attributes.get(TYPE) == "generation"]


def _named(exporter: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [s for s in _finished(exporter) if s.name == name]


# ---------------------------------------------------------------------------
# 每个适配器的每一种调用，对方不回话就在期限上失败
# ---------------------------------------------------------------------------

_CALLS: dict[str, Callable[[ModelClient], Awaitable[Any]]] = {
    "complete": lambda m: m.complete([_user("醒了")]),
    "structured": lambda m: m.structured([_user("醒了")], schema=SCHEMA),
}


@pytest.mark.parametrize("method", list(_CALLS))
async def test_a_gemini_call_that_never_answers_fails_at_the_deadline(
    exported_spans, monkeypatch, method
):
    _gemini(monkeypatch)

    with pytest.raises(CapabilityTimeout, match="gemini-3.7-flash"):
        await _guarded(_CALLS[method](_gemini_adapter()))

    [generation] = _generations(exported_spans)
    assert generation.attributes[LEVEL] == "ERROR"
    assert "gemini-3.7-flash gave no answer within" in generation.attributes[STATUS]


@pytest.mark.parametrize("method", list(_CALLS))
async def test_an_openai_call_that_never_answers_fails_at_the_deadline(
    exported_spans, monkeypatch, method
):
    _openai_never_answers(monkeypatch)

    with pytest.raises(CapabilityTimeout, match="gpt-5.5"):
        await _guarded(_CALLS[method](_openai_adapter()))

    [generation] = _generations(exported_spans)
    assert generation.attributes[LEVEL] == "ERROR"
    assert "gpt-5.5 gave no answer within" in generation.attributes[STATUS]


# ---------------------------------------------------------------------------
# 只有这个期限到点才算"没等到回话"
# ---------------------------------------------------------------------------


async def test_a_timeout_the_sdk_raises_itself_is_not_reported_as_the_deadline(
    exported_spans, monkeypatch
):
    """SDK 自己抛的 TimeoutError 原样往上走：说成"等了 N 秒没回话"就是一笔假账。"""

    async def sdk_gives_up() -> Any:
        raise TimeoutError("sdk read timeout")

    _gemini(monkeypatch, answer=sdk_gives_up)

    with pytest.raises(TimeoutError, match="sdk read timeout"):
        await _guarded(_gemini_adapter().complete([_user("醒了")]))

    [generation] = _generations(exported_spans)
    assert generation.attributes[LEVEL] == "ERROR"
    assert generation.attributes[STATUS] == "TimeoutError: sdk read timeout"


async def test_an_outer_deadline_that_comes_first_stays_the_outer_ones_timeout(
    exported_spans, monkeypatch
):
    """moment 的占用、读书的 ``wait_for`` 这类外层期限先到：报出来的是外层自己的 TimeoutError。

    轮次队列的占用上限（``app.host.turns``）靠"是不是自己这层到点"决定记不记"占住超过上限"；这次调用的
    期限要是冒领了外层的取消，或者吞掉了它，那条日志就错了。取消计数也要还原，外层之后的
    ``asyncio.timeout`` 才判得对。
    """
    monkeypatch.setattr("app.agent.client.MODEL_ANSWER_SECONDS", DEADLINE * 10)
    _gemini(monkeypatch)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(DEADLINE) as outer:
            await _gemini_adapter().complete([_user("醒了")])

    assert outer.expired()
    assert asyncio.current_task().cancelling() == 0
    [generation] = _generations(exported_spans)
    assert generation.attributes[LEVEL] == "ERROR"
    assert generation.attributes[STATUS] == "CancelledError"


# ---------------------------------------------------------------------------
# 她那一轮：Agent.run → 真的 GeminiAdapter → 一直不回话的 SDK
# ---------------------------------------------------------------------------


def _prompt() -> TextPromptClient:
    return TextPromptClient(
        Prompt_Text(
            name="living_life_moment",
            version=1,
            prompt="你是她。",
            config={},
            labels=["production"],
            tags=[],
        )
    )


@pytest.fixture
def life_round(monkeypatch) -> list[dict[str, Any]]:
    """她的一轮怎么调模型就怎么调：prompt 跳过 Langfuse，model_id 解析到真的 GeminiAdapter。"""
    requests = _gemini(monkeypatch)
    langfuse = SimpleNamespace(get_prompt=lambda *_a, **_k: _prompt())
    monkeypatch.setattr("app.agent.prompts._get_client", lambda: langfuse)
    monkeypatch.setattr("app.agent.prompts.get_lane", lambda: None)

    async def _build(_model_id: str) -> ModelClient:
        return _gemini_adapter()

    monkeypatch.setattr("app.agent.core.build_model_client", _build)
    # Agent 层重试之间的退避压到 0，测试不睡。
    monkeypatch.setattr("app.agent.core._BACKOFF_BASE", 0)
    return requests


async def test_her_round_fails_at_the_deadline_with_the_reason_on_the_trace(
    exported_spans, life_round
):
    """她那一轮 ``max_retries`` 是 1：卡住的那次调用到期限就让这一轮失败，交给下一拍重来。"""
    with pytest.raises(CapabilityTimeout):
        await _guarded(
            Agent(LIFE_MOMENT).run(
                [_user("醒了")], call=_her_call(), prompts=await _her_prompts()
            )
        )

    assert len(life_round) == 1
    [root] = _named(exported_spans, "living-life-moment")
    assert root.attributes[LEVEL] == "ERROR"
    assert "gemini-3.7-flash gave no answer within" in root.attributes[STATUS]
    [generation] = _generations(exported_spans)
    assert generation.attributes[LEVEL] == "ERROR"


async def test_a_caller_that_allows_a_retry_gets_one_after_the_deadline(
    exported_spans, life_round
):
    """到期限算可重试的失败，跟 provider 自己的超时一样：允许重试的调用方照它的次数再来。"""
    twice = replace(LIFE_MOMENT, max_retries=2)
    with pytest.raises(CapabilityTimeout):
        await _guarded(
            Agent(twice).run(
                [_user("醒了")], call=_her_call(twice), prompts=await _her_prompts(twice)
            )
        )

    assert len(life_round) == 2
    roots = _named(exported_spans, "living-life-moment")
    assert [r.attributes[LEVEL] for r in roots] == ["ERROR", "ERROR"]
