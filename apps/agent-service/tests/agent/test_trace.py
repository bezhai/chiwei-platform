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
# turn-trace contextvar — unify one (message_id, persona_id) turn's Agent
# root spans into ONE langfuse trace (guards + main), opt-in per turn node.
# ---------------------------------------------------------------------------

from app.agent.trace import current_turn_trace_id, turn_trace  # noqa: E402


def test_current_turn_trace_id_none_outside_turn():
    assert current_turn_trace_id() is None


def test_current_turn_trace_id_deterministic_within_turn():
    with turn_trace("msg-1:persona-7"):
        a = current_turn_trace_id()
        b = current_turn_trace_id()
    assert a is not None
    assert a == b


def test_turn_trace_different_seed_different_id():
    with turn_trace("msg-1:persona-7"):
        a = current_turn_trace_id()
    with turn_trace("msg-1:persona-8"):
        c = current_turn_trace_id()
    assert a != c


def test_turn_trace_resets_on_exit():
    with turn_trace("msg-1:persona-7"):
        assert current_turn_trace_id() is not None
    assert current_turn_trace_id() is None


def test_same_seed_across_scopes_yields_same_id():
    """The unification invariant: run_pre_safety and chat_node compute the same
    seed independently from (message_id, persona_id) → same langfuse trace_id,
    so guards and main land in one trace."""
    with turn_trace("msg-9:persona-3"):
        guard_tid = current_turn_trace_id()
    with turn_trace("msg-9:persona-3"):
        main_tid = current_turn_trace_id()
    assert guard_tid == main_tid


async def test_turn_trace_propagates_through_fan_out_wait():
    """Guards run via fan_out_wait (ensure_future child tasks). The turn seed set
    before the fan-out must reach those tasks so each guard's Agent root span
    attaches to the turn trace, not a separate top-level trace."""
    from app.capabilities.concurrency import fan_out_wait

    seen: list[str | None] = []

    async def _probe() -> None:
        seen.append(current_turn_trace_id())

    with turn_trace("msg-7:persona-2"):
        expected = current_turn_trace_id()
        await fan_out_wait([_probe(), _probe()], timeout_s=5.0)

    assert expected is not None
    assert seen == [expected, expected]


# ---------------------------------------------------------------------------
# model-call generation context: re-parent tool spans under the generation
# that requested them (tool dispatched after the generation span has closed,
# but parent_span_id is still valid).
# ---------------------------------------------------------------------------

from app.agent.trace import (  # noqa: E402
    _capture_current_span_context,
    current_generation_context,
)


def test_capture_current_span_context_inside_and_outside_span():
    from opentelemetry import trace as ot
    from opentelemetry.sdk.trace import TracerProvider

    assert _capture_current_span_context() is None  # no active span
    tracer = TracerProvider().get_tracer("test")
    with tracer.start_as_current_span("gen"):
        cap = _capture_current_span_context()
        assert cap is not None
        assert set(cap) == {"trace_id", "parent_span_id"}
        assert len(cap["trace_id"]) == 32  # langfuse 32-hex trace id
        assert len(cap["parent_span_id"]) == 16  # OTel 16-hex span id
    assert _capture_current_span_context() is None  # reverts after the span
    # capture and TraceContext shape line up so it can be passed straight through
    _ = ot.format_span_id  # imported symbol used by the helper


def test_current_generation_context_none_by_default():
    assert current_generation_context() is None


# ---------------------------------------------------------------------------
# make_session_id — deterministic langfuse session id keyed by
# (lane, actor, date). A persona's whole day of thinking groups into one
# session so we can read a single role's "stream of consciousness" for a day.
# ---------------------------------------------------------------------------

from app.agent.trace import make_session_id  # noqa: E402


def test_make_session_id_stable_for_same_inputs():
    a = make_session_id("prod", "world", "2026-06-04")
    b = make_session_id("prod", "world", "2026-06-04")
    assert a == b


def test_make_session_id_changes_across_days():
    today = make_session_id("prod", "world", "2026-06-04")
    tomorrow = make_session_id("prod", "world", "2026-06-05")
    assert today != tomorrow


def test_make_session_id_differs_per_actor():
    world = make_session_id("prod", "world", "2026-06-04")
    luna = make_session_id("prod", "luna", "2026-06-04")
    assert world != luna


def test_make_session_id_differs_per_lane():
    prod = make_session_id("prod", "world", "2026-06-04")
    coe = make_session_id("coe-x", "world", "2026-06-04")
    assert prod != coe


def test_make_session_id_is_readable():
    """A human reading langfuse should recognise the session: the lane, actor,
    and date are visible in the id, not opaquely hashed away."""
    sid = make_session_id("prod", "world", "2026-06-04")
    assert "prod" in sid
    assert "world" in sid
    assert "2026-06-04" in sid


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
