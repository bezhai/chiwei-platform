"""T4a — contextvars-based AgentContext injection.

Replaces langgraph ``get_runtime(AgentContext).context``. The Agent loop sets
the context before dispatching a tool; the tool body reads it via
``get_context()``. ContextVar semantics give us the two guarantees we need:

  - round-trip: set then get returns the same context,
  - per-task isolation: a context set inside one asyncio task does NOT leak
    into a sibling task (contextvars copy into each spawned task).
"""

from __future__ import annotations

import asyncio

import pytest

from app.agent.context import AgentContext
from app.agent.runtime_context import (
    _current_context,
    agent_context,
    get_context,
    set_context,
)


def test_set_then_get_round_trip():
    ctx = AgentContext(persona_id="p1", chat_id="c1", message_id="m1")
    token = set_context(ctx)
    try:
        assert get_context() is ctx
        assert get_context().persona_id == "p1"
    finally:
        from app.agent.runtime_context import _current_context

        _current_context.reset(token)


def test_get_without_set_raises_lookup_error():
    # No ambient context -> fail fast, do not silently hand back a blank one.
    with pytest.raises(LookupError):
        get_context()


def test_context_manager_sets_and_restores():
    outer = AgentContext(persona_id="outer")
    token = set_context(outer)
    try:
        with agent_context(AgentContext(persona_id="inner")):
            assert get_context().persona_id == "inner"
        # restored to the outer context after the block
        assert get_context().persona_id == "outer"
    finally:
        _current_context.reset(token)


def test_context_manager_restores_unset_state():
    # Entering with no prior context, leaving must restore the unset state
    # (not leave a stale context bound).
    with agent_context(AgentContext(persona_id="scoped")):
        assert get_context().persona_id == "scoped"
    with pytest.raises(LookupError):
        get_context()


async def test_context_isolated_across_async_tasks():
    # Two concurrent tasks each set their own context; neither sees the other's.
    seen: dict[str, str] = {}
    ready = asyncio.Event()

    async def worker(name: str):
        set_context(AgentContext(persona_id=name))
        # let the sibling run between set and read to prove no cross-talk
        await asyncio.sleep(0)
        seen[name] = get_context().persona_id

    await asyncio.gather(worker("a"), worker("b"))
    assert seen == {"a": "a", "b": "b"}
    # the gathering task itself never had a context bound
    _ = ready  # silence unused
    with pytest.raises(LookupError):
        get_context()
