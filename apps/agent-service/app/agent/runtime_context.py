"""Ambient ``AgentContext`` carried via :mod:`contextvars`.

Replaces langgraph's ``get_runtime(AgentContext).context``. The Agent loop sets
the active context before dispatching a tool call; the tool body reads it with
``get_context()``. A ``ContextVar`` gives us per-task isolation for free: each
spawned asyncio task gets its own copy, so concurrent agent runs (e.g. life
tick across personas, parallel sub-agents) never see each other's context.

Usage in the Agent loop (cutover, T4b)::

    with agent_context(ctx):
        result = await dispatch(tools, call)

Usage in a tool body (replaces ``get_runtime(AgentContext).context``)::

    ctx = get_context()
    persona_id = ctx.persona_id
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

from app.agent.context import AgentContext

_current_context: ContextVar[AgentContext] = ContextVar("agent_context")


def set_context(ctx: AgentContext) -> Token[AgentContext]:
    """Bind ``ctx`` as the active agent context, returning a reset token."""
    return _current_context.set(ctx)


def get_context() -> AgentContext:
    """Return the active agent context.

    Raises ``LookupError`` when no context is bound — fail fast rather than
    silently handing back a blank context, which would mask a missing
    ``agent_context(...)`` scope in the Agent loop.
    """
    return _current_context.get()


@contextmanager
def agent_context(ctx: AgentContext) -> Iterator[AgentContext]:
    """Bind ``ctx`` for the duration of the block, then restore prior state.

    Restores whatever was bound before (including the *unset* state), so nested
    scopes and sub-agent delegation don't leak a stale context outward.
    """
    token = set_context(ctx)
    try:
        yield ctx
    finally:
        _current_context.reset(token)
