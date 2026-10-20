"""T2 — langfuse trace helper (generation span).

A small, clean trace module that T3/T4 reuse: T3 wraps each run/extract in a
root span + tool spans, T4 wraps each stream the same way, and every adapter
LLM call wraps itself in a *generation* span via ``generation_span``. This test
pins the helper's shape against the langfuse v3 SDK (``start_generation`` →
``.update(...)`` → ``.end()``), using a mocked langfuse client so no network is
touched.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.trace import generation_span


class _FakeGenCM:
    """Stand-in for langfuse ``start_as_current_generation``'s context manager.

    ``__enter__`` yields the recording generation; ``__exit__`` (how the span is
    now ended) flags it.
    """

    def __init__(self, gen: SimpleNamespace) -> None:
        self.gen = gen

    def __enter__(self) -> SimpleNamespace:
        return self.gen

    def __exit__(self, *_exc: Any) -> bool:
        self.gen.ended = True
        return False


@pytest.fixture
def mock_langfuse(monkeypatch):
    """Patch the trace module's langfuse client getter with a recorder."""
    created: list[SimpleNamespace] = []

    def _start_as_current_generation(**kwargs: Any) -> _FakeGenCM:
        gen = SimpleNamespace(
            start_kwargs=kwargs,
            update_kwargs=None,
            ended=False,
        )
        gen.update = lambda **kw: setattr(gen, "update_kwargs", kw)
        created.append(gen)
        return _FakeGenCM(gen)

    client = SimpleNamespace(
        start_as_current_generation=_start_as_current_generation
    )
    monkeypatch.setattr("app.agent.trace._get_client", lambda: client)
    return created


def test_generation_span_starts_with_model_input(mock_langfuse):
    with generation_span(
        name="llm",
        model="gpt-4o",
        input=[{"role": "user", "content": "hi"}],
        model_parameters={"temperature": 0.7},
    ) as span:
        span.update(output={"content": "hello"}, usage_details={"input": 1})

    assert len(mock_langfuse) == 1
    gen = mock_langfuse[0]
    assert gen.start_kwargs["name"] == "llm"
    assert gen.start_kwargs["model"] == "gpt-4o"
    assert gen.start_kwargs["input"] == [{"role": "user", "content": "hi"}]
    assert gen.start_kwargs["model_parameters"] == {"temperature": 0.7}


def test_generation_span_records_output_and_ends(mock_langfuse):
    with generation_span(name="llm", model="gpt-4o", input=[]) as span:
        span.update(output={"content": "hello"}, usage_details={"input": 5})

    gen = mock_langfuse[0]
    assert gen.update_kwargs["output"] == {"content": "hello"}
    assert gen.update_kwargs["usage_details"] == {"input": 5}
    assert gen.ended is True


def test_generation_span_ends_even_on_exception(mock_langfuse):
    with pytest.raises(ValueError):
        with generation_span(name="llm", model="gpt-4o", input=[]):
            raise ValueError("boom")

    gen = mock_langfuse[0]
    assert gen.ended is True


def test_generation_span_swallows_langfuse_failure(monkeypatch):
    """Tracing must never break the LLM call: a failing client is tolerated."""

    def _boom() -> Any:
        raise RuntimeError("langfuse down")

    monkeypatch.setattr("app.agent.trace._get_client", _boom)

    # Should not raise; span becomes a no-op
    with generation_span(name="llm", model="gpt-4o", input=[]) as span:
        span.update(output="x")


def test_generation_span_swallows_update_failure(monkeypatch):
    """A throwing ``span.update`` must NOT fail the (already-succeeded) call.

    The LLM response is already in hand by the time we record output/usage; a
    langfuse serialisation error there must be swallowed, not propagated.
    """

    class _BadUpdateCM:
        def __enter__(self) -> Any:
            def _bad_update(**_kw: Any) -> None:
                raise RuntimeError("langfuse update exploded")

            return SimpleNamespace(update=_bad_update)

        def __exit__(self, *_exc: Any) -> bool:
            return False

    client = SimpleNamespace(start_as_current_generation=lambda **_k: _BadUpdateCM())
    monkeypatch.setattr("app.agent.trace._get_client", lambda: client)

    # update() raising inside the block must not surface
    with generation_span(name="llm", model="gpt-4o", input=[]) as span:
        span.update(output="x", usage_details={"input": 1})


def test_generation_span_swallows_end_failure(monkeypatch):
    """A throwing context-exit (the span's ``end``) must not surface either."""

    class _BadExitCM:
        def __enter__(self) -> Any:
            return SimpleNamespace(update=lambda **_kw: None)

        def __exit__(self, *_exc: Any) -> bool:
            raise RuntimeError("end exploded")

    client = SimpleNamespace(start_as_current_generation=lambda **_k: _BadExitCM())
    monkeypatch.setattr("app.agent.trace._get_client", lambda: client)

    with generation_span(name="llm", model="gpt-4o", input=[]):
        pass


# ---------------------------------------------------------------------------
# under_last_generation —— 在它里面开的 span 挂在最近一次模型调用下面：工具在模型
# 调用结束之后才派发，那个 generation 已经结束了，但仍能当父节点。
# ---------------------------------------------------------------------------

from app.agent.trace import under_last_generation  # noqa: E402


async def test_a_span_opened_under_the_last_generation_hangs_under_it(
    tracer, exported_spans
):
    with tracer.start_as_current_span("root"):
        with generation_span(name="llm", model="m", input=[]):
            pass
        with under_last_generation(), tracer.start_as_current_span("tool") as tool:
            pass

    [generation] = exported_spans.get_finished_spans()
    assert tool.parent is not None
    assert tool.parent.span_id == generation.context.span_id
    assert tool.get_span_context().trace_id == generation.context.trace_id


# ---------------------------------------------------------------------------
# collect_usage — accumulate one Agent.run's token usage into a contextvar so
# world/life收口 can record it to durable PG, INDEPENDENT of langfuse (the
# whole point: langfuse drops durable-tool traces, PG must not). The hook lives
# in _SafeSpan.update / _NoOpSpan.update — the sole funnel all three adapter
# calls (complete / stream / structured) pass through.
# ---------------------------------------------------------------------------

from app.agent.trace import collect_usage  # noqa: E402


def test_collect_usage_yields_zero_initialised_accumulator(mock_langfuse):
    with collect_usage() as usage:
        pass
    assert usage == {
        "input": 0,
        "output": 0,
        "total": 0,
        "cache_read_input_tokens": 0,
        "calls": 0,
    }


def test_collect_usage_accumulates_via_safespan_update(mock_langfuse):
    """A _SafeSpan.update carrying usage_details accumulates into the collector;
    each such update counts one model call."""
    with collect_usage() as usage:
        with generation_span(name="llm", model="m", input=[]) as span:
            span.update(
                output="a",
                usage_details={"input": 10, "output": 4, "total": 14},
            )
        with generation_span(name="llm", model="m", input=[]) as span:
            span.update(
                output="b",
                usage_details={
                    "input": 6,
                    "output": 2,
                    "total": 8,
                    "cache_read_input_tokens": 3,
                },
            )
    assert usage == {
        "input": 16,
        "output": 6,
        "total": 22,
        "cache_read_input_tokens": 3,
        "calls": 2,
    }


def test_collect_usage_update_without_usage_details_does_not_count_call(
    mock_langfuse,
):
    """An update that only records output (no usage_details) is not a model call
    and must not bump the call counter or any token dimension."""
    with collect_usage() as usage:
        with generation_span(name="llm", model="m", input=[]) as span:
            span.update(output="just output")
    assert usage["calls"] == 0
    assert usage["input"] == 0
    assert usage["total"] == 0


def test_collect_usage_accumulates_when_langfuse_unavailable(monkeypatch):
    """The whole point of this刀: token usage comes from the LLM response, not
    langfuse. When langfuse is down the span is a _NoOpSpan — its update must
    STILL accumulate, so cost observability survives langfuse loss."""

    def _boom() -> Any:
        raise RuntimeError("langfuse down")

    monkeypatch.setattr("app.agent.trace._get_client", _boom)

    with collect_usage() as usage:
        with generation_span(name="llm", model="m", input=[]) as span:
            span.update(
                output="x",
                usage_details={"input": 7, "output": 3, "total": 10},
            )
    assert usage == {
        "input": 7,
        "output": 3,
        "total": 10,
        "cache_read_input_tokens": 0,
        "calls": 1,
    }


def test_accumulate_usage_outside_collect_is_safe(mock_langfuse):
    """No active collector → an update with usage_details must not raise."""
    with generation_span(name="llm", model="m", input=[]) as span:
        span.update(output="x", usage_details={"input": 1, "total": 1})
    # reaching here without error is the assertion


def test_collect_usage_resets_on_exit(mock_langfuse):
    """Leaving the contextmanager clears the collector so a later update outside
    it does not leak into a stale accumulator."""
    with collect_usage() as first:
        with generation_span(name="llm", model="m", input=[]) as span:
            span.update(output="x", usage_details={"input": 5, "total": 5})
    assert first["input"] == 5

    # Outside collect now: an update must not mutate the old dict.
    with generation_span(name="llm", model="m", input=[]) as span:
        span.update(output="y", usage_details={"input": 99, "total": 99})
    assert first["input"] == 5


# ---------------------------------------------------------------------------
# collect_usage — ContextVar leak boundary. ContextVars copy into tasks spawned
# by asyncio.create_task, so a tool that fires a background LLM call inside a run
# would inherit the parent's collector and bill someone else's tokens to this
# round. world/life tools don't do this today; these tests pin the boundary so a
# future background-LLM addition sees the contract.
# ---------------------------------------------------------------------------

from app.agent.trace import _usage_collector  # noqa: E402


async def test_collect_usage_collector_cleared_after_scope_in_async():
    """After collect_usage exits (in an async context), the collector ContextVar
    is back to None — a later accumulate in this task does not leak into the
    finished round's dict. This is the leak-prevention invariant the cost刀
    relies on: one round's collector must not survive into the next."""
    assert _usage_collector.get() is None  # clean before
    with collect_usage() as first:
        with generation_span(name="llm", model="m", input=[]) as span:
            span.update(output="x", usage_details={"input": 5, "total": 5})
        assert _usage_collector.get() is first  # active inside scope
    # The whole point: collector cleared on exit, no stale accumulator lingers.
    assert _usage_collector.get() is None


async def test_collect_usage_collector_inherited_by_create_task_child(
    mock_langfuse,
):
    """DOCUMENTED CURRENT BEHAVIOR (boundary marker, NOT a desired feature):

    asyncio.create_task snapshots the current contextvars, so a child task
    spawned *inside* a collect_usage scope inherits the SAME collector object.
    Today no world/life tool spawns a background LLM call inside a run, so this
    is harmless. But if one ever does, its tokens would accumulate into THIS
    round's collector. This test exists so whoever adds such a path trips it and
    sees the boundary, rather than silently mis-billing a round.
    """
    import asyncio

    seen: list[dict | None] = []

    async def _child() -> None:
        # The child sees the parent's collector (ContextVar copied at task spawn)
        # and an accumulate here lands in the parent's round dict.
        seen.append(_usage_collector.get())
        with generation_span(name="llm", model="m", input=[]) as span:
            span.update(output="bg", usage_details={"input": 11, "total": 11})

    with collect_usage() as usage:
        task = asyncio.create_task(_child())
        await task

    # Child inherited the same collector object the parent yielded.
    assert seen == [usage]
    # And the child's background tokens were billed into this round — documenting
    # exactly the mis-billing a future background-LLM tool would cause.
    assert usage["input"] == 11
    assert usage["total"] == 11
    assert usage["calls"] == 1


# ---------------------------------------------------------------------------
# separate_trace —— 一个 agent 在工具里调另一个 agent 时，里面那个另起一条 trace
# ---------------------------------------------------------------------------

import opentelemetry.trace as otel_trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402

from app.agent.trace import separate_trace  # noqa: E402


@pytest.fixture
def tracer():
    """一个本地的 OTel tracer：不碰全局 provider，span 照样进当前上下文。"""
    return TracerProvider().get_tracer("test")


def test_separate_trace_starts_spans_outside_the_current_trace(tracer):
    with tracer.start_as_current_span("outer") as outer:
        with separate_trace():
            assert not otel_trace.get_current_span().get_span_context().is_valid
            with tracer.start_as_current_span("inner") as inner:
                pass
        assert otel_trace.get_current_span() is outer

    assert inner.parent is None
    assert inner.get_span_context().trace_id != outer.get_span_context().trace_id


async def test_separate_trace_leaves_the_outer_model_call_as_it_was(
    tracer, exported_spans
):
    """里面看不到外面那次模型调用；离开之后，外面那一轮接下来的工具 span 仍然挂在它自己的
    模型调用下面，而不是里面那次。"""
    with tracer.start_as_current_span("outer"):
        with generation_span(name="outer-llm", model="m", input=[]):
            pass

        with separate_trace():
            with under_last_generation(), tracer.start_as_current_span(
                "inner-tool"
            ) as inner_tool:
                pass
            with generation_span(name="inner-llm", model="m", input=[]):
                pass

        with under_last_generation(), tracer.start_as_current_span(
            "outer-tool"
        ) as outer_tool:
            pass

    generations = {s.name: s for s in exported_spans.get_finished_spans()}
    assert inner_tool.parent is None
    assert outer_tool.parent is not None
    assert outer_tool.parent.span_id == generations["outer-llm"].context.span_id


# ---------------------------------------------------------------------------
# rendered_from —— 作用域里开的 generation 关联这次调用编译的 prompt；另起的 trace
# 不带外面那一轮的 prompt 进去，离开之后外面那一轮照旧关联它自己的。
# ---------------------------------------------------------------------------

from langfuse._client.attributes import LangfuseOtelSpanAttributes  # noqa: E402
from langfuse.api import Prompt_Text  # noqa: E402
from langfuse.model import TextPromptClient  # noqa: E402

from app.agent.trace import rendered_from  # noqa: E402


def _text_prompt(name: str) -> TextPromptClient:
    return TextPromptClient(
        Prompt_Text(
            name=name, version=1, prompt="段落。", config={}, labels=["production"], tags=[]
        )
    )


async def test_a_model_call_links_its_prompt_only_when_it_is_rendered_from_one(
    exported_spans,
):
    """A generation links one prompt; one rendered from several segments links none of them, or
    that segment's use would read as the whole prompt's."""
    core, order = _text_prompt("world_round/core"), _text_prompt("world_round/order")
    with rendered_from((core, order)):
        with generation_span(name="segmented-llm", model="m", input=[]):
            pass
    with rendered_from((core,)):
        with generation_span(name="single-llm", model="m", input=[]):
            pass

    linked = {
        s.name: s.attributes.get(LangfuseOtelSpanAttributes.OBSERVATION_PROMPT_NAME)
        for s in exported_spans.get_finished_spans()
    }
    assert linked == {"segmented-llm": None, "single-llm": "world_round/core"}


async def test_separate_trace_does_not_carry_the_outer_prompt_in(exported_spans):
    world_round = TextPromptClient(
        Prompt_Text(
            name="world_round",
            version=3,
            prompt="你是这个世界。",
            config={},
            labels=["production"],
            tags=[],
        )
    )
    with rendered_from((world_round,)):
        with separate_trace():
            with generation_span(name="inner-llm", model="m", input=[]):
                pass
        with generation_span(name="outer-llm", model="m", input=[]):
            pass

    linked = {
        s.name: s.attributes.get(LangfuseOtelSpanAttributes.OBSERVATION_PROMPT_NAME)
        for s in exported_spans.get_finished_spans()
    }
    assert linked == {"inner-llm": None, "outer-llm": "world_round"}
