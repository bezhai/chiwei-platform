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
