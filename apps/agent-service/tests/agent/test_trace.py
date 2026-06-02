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


@pytest.fixture
def mock_langfuse(monkeypatch):
    """Patch the trace module's langfuse client getter with a recorder."""
    created: list[SimpleNamespace] = []

    def _start_generation(**kwargs: Any) -> SimpleNamespace:
        gen = SimpleNamespace(
            start_kwargs=kwargs,
            update_kwargs=None,
            ended=False,
        )

        def _update(**kw: Any) -> None:
            gen.update_kwargs = kw

        def _end(**kw: Any) -> None:
            gen.ended = True

        gen.update = _update
        gen.end = _end
        created.append(gen)
        return gen

    client = SimpleNamespace(start_generation=_start_generation)
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

    def _start_generation(**_kwargs: Any) -> Any:
        def _bad_update(**_kw: Any) -> None:
            raise RuntimeError("langfuse update exploded")

        return SimpleNamespace(update=_bad_update, end=lambda **_kw: None)

    client = SimpleNamespace(start_generation=_start_generation)
    monkeypatch.setattr("app.agent.trace._get_client", lambda: client)

    # update() raising inside the block must not surface
    with generation_span(name="llm", model="gpt-4o", input=[]) as span:
        span.update(output="x", usage_details={"input": 1})


def test_generation_span_swallows_end_failure(monkeypatch):
    """A throwing ``end`` on context exit must not surface either."""

    def _start_generation(**_kwargs: Any) -> Any:
        def _bad_end(**_kw: Any) -> None:
            raise RuntimeError("end exploded")

        return SimpleNamespace(update=lambda **_kw: None, end=_bad_end)

    client = SimpleNamespace(start_generation=_start_generation)
    monkeypatch.setattr("app.agent.trace._get_client", lambda: client)

    with generation_span(name="llm", model="gpt-4o", input=[]):
        pass
