"""Manual langfuse spans for the thinking core.

LangChain's ``CallbackHandler`` auto-instrumented the old agent. The self-built
core埋 spans by hand. This module is the *generation*-span ground floor: every
adapter LLM call (T2) wraps itself in one ``generation_span``; T3/T4 build the
``run``/``stream``/``extract`` root span and tool spans on top of the same
langfuse v3 client.

Why a generation span is *unconditional* (spec §Key design decisions): the
legacy ``update_trace=False`` on guard / ``deep_research`` paths means "do not
overwrite the parent trace's name / metadata / IO", **not** "do not trace". The
generation span must always exist or we violate "every LLM call is traced".
So this helper has no ``update_trace`` knob — it always opens a span. The
parent-trace overwrite decision lives one layer up (T3/T4), via langfuse's
``update_current_trace``.

Tracing must never break the LLM call. If langfuse is unconfigured or throws,
the span degrades to a no-op and the call proceeds.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from langfuse import Langfuse

from app.infra.config import settings

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

_client: Langfuse | None = None


# ---------------------------------------------------------------------------
# Turn-trace context: unify one chat turn's Agent spans into one langfuse trace
# ---------------------------------------------------------------------------

# A chat turn's per-LLM operations (pre-check guards via emit_and_wait, the main
# stream) run in *separate* @node / async-task contexts, so each Agent root span
# would otherwise open its own top-level langfuse trace. The OTel current-span
# does not propagate across those dataflow boundaries. We instead derive a
# deterministic langfuse trace_id from a per-turn seed (``message_id:persona_id``)
# and attach every Agent root span to it, so guards + main land in ONE trace.
#
# This is OPT-IN: only per-turn @nodes (run_pre_safety, chat_node) enter
# ``turn_trace`` from their request's (message_id, persona_id). Debounced
# post-actions (afterthought / voice) deliberately do NOT — they are
# (chat, persona) aggregations, not a turn, and must stay separate traces. A
# debounce-propagated runtime trace_id would have leaked them into a turn trace,
# which is exactly why we don't seed from the runtime trace_id.
_turn_trace_seed: ContextVar[str | None] = ContextVar(
    "agent_turn_trace_seed", default=None
)


@contextmanager
def turn_trace(seed: str) -> Iterator[None]:
    """Mark the current async scope as one chat turn keyed by ``seed``.

    Every ``Agent`` root span opened inside this scope attaches to the same
    langfuse trace (derived deterministically from ``seed``). Restores the
    previous value on exit (success or exception).
    """
    token = _turn_trace_seed.set(seed)
    try:
        yield
    finally:
        _turn_trace_seed.reset(token)


def current_turn_trace_id() -> str | None:
    """The langfuse trace_id for the active turn, or None when outside any turn.

    Deterministic in the seed: two scopes that compute the same
    ``message_id:persona_id`` (e.g. run_pre_safety and chat_node) get the same
    trace_id and therefore the same trace.
    """
    seed = _turn_trace_seed.get()
    if not seed:
        return None
    return Langfuse.create_trace_id(seed=seed)


def _get_client() -> Langfuse:
    """Lazily initialise and return the langfuse singleton client."""
    global _client
    if _client is None:
        _client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return _client


class _NoOpSpan:
    """A generation span that does nothing — used when langfuse is unavailable."""

    def update(self, **_kwargs: Any) -> None:
        pass

    def end(self, **_kwargs: Any) -> None:
        pass


class _SafeSpan:
    """Wraps a langfuse generation so ``update`` / ``end`` never raise.

    The LLM response is already in hand by the time callers record output /
    usage, so a langfuse serialisation or transport error there must NOT fail
    the (successful) LLM call. Every delegated call is swallowed and logged.
    """

    def __init__(self, generation: Any) -> None:
        self._gen = generation

    def update(self, **kwargs: Any) -> None:
        try:
            self._gen.update(**kwargs)
        except Exception as exc:
            logger.warning("langfuse generation update failed: %s", exc)

    def end(self, **kwargs: Any) -> None:
        try:
            self._gen.end(**kwargs)
        except Exception as exc:
            logger.warning("langfuse generation end failed: %s", exc)


@contextmanager
def generation_span(
    *,
    name: str,
    model: str,
    input: Any,
    model_parameters: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open a langfuse generation span around one LLM call.

    Yields the generation object; the caller records the result with
    ``span.update(output=..., usage_details=...)`` once the response arrives.
    The span is closed (``.end()``) on context exit — including on exception,
    so a failed call still produces a (truncated) span rather than vanishing.

    A langfuse failure (unconfigured keys, network) degrades to a no-op span;
    the wrapped LLM call always proceeds.
    """
    try:
        gen = _get_client().start_generation(
            name=name,
            model=model,
            input=input,
            model_parameters=model_parameters,
            metadata=metadata,
        )
    except Exception as exc:
        logger.warning("langfuse generation span unavailable: %s", exc)
        yield _NoOpSpan()
        return

    span = _SafeSpan(gen)
    try:
        yield span
    finally:
        span.end()
