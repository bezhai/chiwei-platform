"""每一次 LLM 调用花了多少，Prometheus 上必须看得见。

四类用量（input / output / 缓存 / 思考）各自是一个维度，而且**缓存要分得出三种状态**：
provider 报了非零、报了零、根本没报。压成同一个形状正是先前误判"命中恒为 0"的原因 ——
两个 token 计数器相除得到的是一个比例，分母里有多少次调用根本没有缓存数据看不出来。

采集点在 span 层（``app.agent.trace``）而不是 ``collect_usage``：那个累加器只包了
world / life 收口那几处，chat / guard / extract 不经过它。langfuse 不可用时走
``_NoOpSpan``，那条路也必须记 —— 否则线上 langfuse 一挂，指标跟着瞎。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import REGISTRY, generate_latest

from app.agent.trace import (
    LLM_TOKENS,
    LLM_USAGE_REPORTS,
    generation_span,
)

pytestmark = pytest.mark.unit


def _tokens(model: str, kind: str, lane: str = "prod") -> float:
    return (
        REGISTRY.get_sample_value(
            "llm_tokens_total", {"lane": lane, "model": model, "kind": kind}
        )
        or 0.0
    )


def _reports(model: str, kind: str, report: str, lane: str = "prod") -> float:
    return (
        REGISTRY.get_sample_value(
            "llm_usage_reports_total",
            {"lane": lane, "model": model, "kind": kind, "report": report},
        )
        or 0.0
    )


@pytest.fixture
def langfuse_down(monkeypatch):
    """langfuse 不可用 —— span 降级成 no-op，指标那一路必须照记。"""

    def _boom() -> Any:
        raise RuntimeError("langfuse is down")

    monkeypatch.setattr("app.agent.trace._get_client", _boom)


@pytest.fixture
def langfuse_up(monkeypatch):
    """langfuse 正常 —— 走 ``_SafeSpan``。"""

    class _CM:
        def __enter__(self) -> Any:
            return SimpleNamespace(update=lambda **kw: None, end=lambda **kw: None)

        def __exit__(self, *_exc: Any) -> bool:
            return False

    monkeypatch.setattr(
        "app.agent.trace._get_client",
        lambda: SimpleNamespace(start_as_current_generation=lambda **kw: _CM()),
    )


def _record(model: str, usage: dict[str, int] | None) -> None:
    with generation_span(name=model, model=model, input=[]) as span:
        span.update(output={}, usage_details=usage)


# ---------------------------------------------------------------------------
# 四类用量
# ---------------------------------------------------------------------------


def test_all_four_kinds_of_usage_land_on_the_endpoint(langfuse_up):
    model = "usage-four-kinds"
    _record(
        model,
        {
            "input": 12000,
            "output": 300,
            "total": 12500,
            "cache_read_input_tokens": 9000,
            "thinking_tokens": 200,
        },
    )

    assert _tokens(model, "input") == 12000
    assert _tokens(model, "output") == 300
    assert _tokens(model, "cached") == 9000
    assert _tokens(model, "thinking") == 200

    exposed = generate_latest().decode()
    assert "llm_tokens_total" in exposed
    assert "llm_usage_reports_total" in exposed


def test_the_lane_is_on_the_metric(monkeypatch, langfuse_up):
    monkeypatch.setenv("LANE", "coe-living")
    model = "usage-lane"
    _record(model, {"input": 7, "output": 1, "total": 8})

    assert _tokens(model, "input", lane="coe-living") == 7
    assert _tokens(model, "input") == 0


# ---------------------------------------------------------------------------
# 缓存的三种状态
# ---------------------------------------------------------------------------


def test_a_reported_hit_a_reported_zero_and_no_report_are_told_apart(langfuse_up):
    model = "usage-cache-states"
    _record(model, {"input": 100, "output": 1, "cache_read_input_tokens": 60})
    _record(model, {"input": 100, "output": 1, "cache_read_input_tokens": 0})
    _record(model, {"input": 100, "output": 1})

    assert _reports(model, "cached", "nonzero") == 1
    assert _reports(model, "cached", "zero") == 1
    assert _reports(model, "cached", "absent") == 1
    # 三次调用都在同一个分母上：按"有没有缓存数据"分组之后仍然加得回 3
    assert sum(
        _reports(model, "cached", r) for r in ("nonzero", "zero", "absent")
    ) == 3
    assert _tokens(model, "cached") == 60


def test_a_call_that_reported_no_usage_at_all_is_still_one_call(langfuse_up):
    model = "usage-nothing-reported"
    _record(model, None)

    for kind in ("input", "output", "cached", "thinking"):
        assert _reports(model, kind, "absent") == 1
        assert _tokens(model, kind) == 0


# ---------------------------------------------------------------------------
# 一次调用只计一次；langfuse 死了也照记
# ---------------------------------------------------------------------------


def test_one_call_is_counted_once_however_often_the_span_is_updated(langfuse_up):
    """流式那一路把用量累到最后一次 update 上，重复 update 不能重复入账。"""
    model = "usage-counted-once"
    with generation_span(name=model, model=model, input=[]) as span:
        span.update(output={"text": "部分"}, usage_details={"input": 50, "output": 2})
        span.update(output={"text": "全部"}, usage_details={"input": 50, "output": 2})

    assert _tokens(model, "input") == 50
    assert _reports(model, "input", "nonzero") == 1


def test_the_usage_is_recorded_even_when_langfuse_is_gone(langfuse_down):
    """token 来自 LLM response，跟 langfuse 死活无关 —— 降级那条路也得记。"""
    model = "usage-langfuse-down"
    _record(model, {"input": 31, "output": 4, "cache_read_input_tokens": 0})

    assert _tokens(model, "input") == 31
    assert _reports(model, "cached", "zero") == 1


def test_the_metric_names_follow_the_repo_convention():
    assert LLM_TOKENS._name == "llm_tokens"
    assert LLM_USAGE_REPORTS._name == "llm_usage_reports"
    assert sorted(LLM_TOKENS._labelnames) == ["kind", "lane", "model"]
    assert sorted(LLM_USAGE_REPORTS._labelnames) == [
        "kind",
        "lane",
        "model",
        "report",
    ]


# ---------------------------------------------------------------------------
# 真流式：一次调用一次账
# ---------------------------------------------------------------------------


def _streamed_chunk(text: str, prompt: int, candidates: int, thinking: int) -> Any:
    """一块流式返回。Gemini 每块报的是**累计**用量，不是增量。"""
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text=text,
                            thought=False,
                            function_call=None,
                            inline_data=None,
                            thought_signature=None,
                        )
                    ],
                    role="model",
                ),
                finish_reason=None,
                index=0,
            )
        ],
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=candidates,
            total_token_count=prompt + candidates + thinking,
            cached_content_token_count=0,
            thoughts_token_count=thinking,
        ),
    )


async def test_a_streamed_call_is_billed_once_not_once_per_chunk(
    monkeypatch, langfuse_up
):
    from app.agent.adapters.gemini import GeminiAdapter
    from app.agent.neutral import Message, Role

    chunks = [
        _streamed_chunk("你", 1000, 1, 20),
        _streamed_chunk("好", 1000, 2, 20),
        _streamed_chunk("。", 1000, 3, 20),
    ]

    class _Client:
        def __init__(self, **_kw: Any) -> None:
            async def _stream(**_k: Any) -> Any:
                async def _gen() -> Any:
                    for c in chunks:
                        yield c

                return _gen()

            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content_stream=_stream)
            )

    monkeypatch.setattr("app.agent.adapters.gemini.genai.Client", _Client)
    model = "usage-streamed-once"
    adapter = GeminiAdapter(model_name=model, api_key="k", base_url="https://g")

    async for _ in adapter.stream([Message(role=Role.USER, content="hi")]):
        pass

    # 最后一块的累计值各记一次，不是三块相加
    assert _tokens(model, "input") == 1000
    assert _tokens(model, "output") == 3
    assert _tokens(model, "thinking") == 20
    assert _reports(model, "input", "nonzero") == 1
    assert _reports(model, "cached", "zero") == 1


# ---------------------------------------------------------------------------
# 流中途断了：已经报过的用量不能跟着一起丢
#
# 用量是在流跑完之后一次性记到 span 上的，所以流抛异常 / 消费方中途走人的时候，
# 那次调用在指标上完全不存在 —— token 没记，三个 report 状态也没记。去重（一次调用
# 只计一次）救不了这个：它只能拦住重复的 update，补不出没执行的那一次。
# ---------------------------------------------------------------------------


def _gemini_stream(
    monkeypatch, model: str, chunks: list[Any], *, boom: Exception | None = None
) -> Any:
    from app.agent.adapters.gemini import GeminiAdapter

    class _Client:
        def __init__(self, **_kw: Any) -> None:
            async def _stream(**_k: Any) -> Any:
                async def _gen() -> Any:
                    for c in chunks:
                        yield c
                    if boom is not None:
                        raise boom

                return _gen()

            self.aio = SimpleNamespace(
                models=SimpleNamespace(generate_content_stream=_stream)
            )

    monkeypatch.setattr("app.agent.adapters.gemini.genai.Client", _Client)
    return GeminiAdapter(model_name=model, api_key="k", base_url="https://g")


def _openai_chunk(text: str, prompt: int, completion: int) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=text, reasoning_content=None, tool_calls=None
                ),
                finish_reason=None,
                index=0,
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            prompt_tokens_details=None,
            completion_tokens_details=None,
        ),
    )


def _openai_stream(
    monkeypatch, model: str, chunks: list[Any], *, boom: Exception | None = None
) -> Any:
    from app.agent.adapters.openai import OpenAIAdapter

    class _Client:
        def __init__(self, **_kw: Any) -> None:
            async def _create(**_k: Any) -> Any:
                async def _gen() -> Any:
                    for c in chunks:
                        yield c
                    if boom is not None:
                        raise boom

                return _gen()

            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=_create)
            )

    monkeypatch.setattr("app.agent.adapters.openai.AsyncOpenAI", _Client)
    return OpenAIAdapter(model_name=model, api_key="k", base_url="https://o")


def _user() -> Any:
    from app.agent.neutral import Message, Role

    return [Message(role=Role.USER, content="hi")]


async def test_a_gemini_stream_that_blew_up_books_what_it_already_reported(
    monkeypatch, langfuse_up
):
    model = "usage-gemini-stream-exploded"
    adapter = _gemini_stream(
        monkeypatch,
        model,
        [_streamed_chunk("你", 100, 1, 5)],
        boom=RuntimeError("connection reset"),
    )

    with pytest.raises(RuntimeError):
        async for _ in adapter.stream(_user()):
            pass

    assert _tokens(model, "input") == 100
    assert _tokens(model, "output") == 1
    assert _tokens(model, "thinking") == 5
    assert _reports(model, "input", "nonzero") == 1


async def test_a_gemini_stream_the_consumer_walked_away_from_is_still_booked(
    monkeypatch, langfuse_up
):
    """消费方看到 content_filter 就撒手不再拉了——那次调用照样得入账。"""
    model = "usage-gemini-stream-cancelled"
    adapter = _gemini_stream(
        monkeypatch,
        model,
        [_streamed_chunk("你", 100, 1, 5), _streamed_chunk("好", 100, 2, 5)],
    )

    gen = adapter.stream(_user())
    await gen.__anext__()
    await gen.aclose()

    assert _tokens(model, "input") == 100
    assert _reports(model, "input", "nonzero") == 1


async def test_a_gemini_stream_that_died_before_any_usage_is_still_one_call(
    monkeypatch, langfuse_up
):
    model = "usage-gemini-stream-nothing"
    adapter = _gemini_stream(
        monkeypatch, model, [], boom=RuntimeError("connection reset")
    )

    with pytest.raises(RuntimeError):
        async for _ in adapter.stream(_user()):
            pass

    for kind in ("input", "output", "cached", "thinking"):
        assert _reports(model, kind, "absent") == 1


async def test_an_openai_stream_that_blew_up_books_what_it_already_reported(
    monkeypatch, langfuse_up
):
    model = "usage-openai-stream-exploded"
    adapter = _openai_stream(
        monkeypatch,
        model,
        [_openai_chunk("你", 100, 1)],
        boom=RuntimeError("connection reset"),
    )

    with pytest.raises(RuntimeError):
        async for _ in adapter.stream(_user()):
            pass

    assert _tokens(model, "input") == 100
    assert _tokens(model, "output") == 1
    assert _reports(model, "input", "nonzero") == 1


async def test_an_openai_stream_the_consumer_walked_away_from_is_still_booked(
    monkeypatch, langfuse_up
):
    model = "usage-openai-stream-cancelled"
    adapter = _openai_stream(
        monkeypatch,
        model,
        [_openai_chunk("你", 100, 1), _openai_chunk("好", 100, 2)],
    )

    gen = adapter.stream(_user())
    await gen.__anext__()
    await gen.aclose()

    assert _tokens(model, "input") == 100
    assert _reports(model, "input", "nonzero") == 1
