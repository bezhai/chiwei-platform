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

import opentelemetry.trace as _otel_trace
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

# The trace-level name every root span in a turn writes. A turn's guards, main
# stream, and post-safety are separate root spans on one trace; langfuse derives
# the trace name from whichever root span is ingested last (post-safety, which
# runs last), so the whole trace would otherwise read "post-safety-check". Each
# root span writes THIS name instead, so the trace name is stable regardless of
# ingestion order. This is the trace-level name only — each span keeps its own
# observation name (pre-nsfw-check / main / post-safety-check / ...).
TURN_TRACE_NAME = "chat-turn"


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


def make_session_id(lane: str, actor: str, date: str) -> str:
    """Deterministic, readable langfuse session id for one actor's day.

    Groups every LLM call an actor makes on a given day into a single langfuse
    session, so a role's "stream of consciousness" for that day reads as one
    thread. Same ``(lane, actor, date)`` always yields the same id; the date
    rolls the session daily, and a different lane / actor never collides.

    ``actor`` is "world" or a persona_id; ``date`` is ``YYYY-MM-DD``. The id is
    left human-readable (lane / actor / date visible) rather than hashed so the
    session is recognisable when browsing langfuse.
    """
    return f"{lane}:{actor}:{date}"


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


# ---------------------------------------------------------------------------
# Model-call generation context: nest tool spans under their model call
# ---------------------------------------------------------------------------

# In a ReAct loop the model call's generation span has already closed by the
# time the loop dispatches the tools it requested, so a tool span would nest
# flat under the agent root. We snapshot the generation's span context here and
# let the loop re-parent each tool span under it via parent_span_id (a closed
# span is still a valid parent), so the trace reads model-call → its tools.
_current_generation_ctx: ContextVar[dict[str, str] | None] = ContextVar(
    "agent_current_generation_ctx", default=None
)


def _capture_current_span_context() -> dict[str, str] | None:
    """Snapshot the current OTel span as a langfuse TraceContext, or None.

    Returns ``{"trace_id", "parent_span_id"}`` (32-/16-hex, the shapes langfuse
    TraceContext wants) for the active span, or None when no valid span is
    current (langfuse unavailable / outside any span).
    """
    span = _otel_trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is None or not ctx.is_valid:
        return None
    return {
        "trace_id": _otel_trace.format_trace_id(ctx.trace_id),
        "parent_span_id": _otel_trace.format_span_id(ctx.span_id),
    }


def current_generation_context() -> dict[str, str] | None:
    """The most recent model call's TraceContext in this task, or None.

    Set as a side effect of ``generation_span`` (never reset — it is overwritten
    by the next model call and dies with the task; not resetting also avoids a
    ContextVar token being reset across an async-generator yield). The ReAct loop
    reads it to parent each tool span under the model call that requested it.
    """
    return _current_generation_ctx.get()


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

    Opened *as the current span* so the loop can snapshot its context (for tool
    re-parenting) and so anything nested during the call hangs under it.
    """
    try:
        cm = _get_client().start_as_current_generation(
            name=name,
            model=model,
            input=input,
            model_parameters=model_parameters,
            metadata=metadata,
        )
        gen = cm.__enter__()
    except Exception as exc:
        logger.warning("langfuse generation span unavailable: %s", exc)
        yield _NoOpSpan()
        return

    # Record this generation's span context so a tool span dispatched right after
    # (in the ReAct loop, once this generation has closed) re-parents under it.
    _current_generation_ctx.set(_capture_current_span_context())

    span = _SafeSpan(gen)
    body_exc: BaseException | None = None
    try:
        yield span
    except BaseException as exc:  # noqa: BLE001 - re-raised after closing span
        body_exc = exc
        raise
    finally:
        try:
            if body_exc is not None:
                cm.__exit__(type(body_exc), body_exc, body_exc.__traceback__)
            else:
                cm.__exit__(None, None, None)
        except Exception as exc:  # pragma: no cover - span teardown failure
            logger.warning("langfuse generation span teardown failed: %s", exc)
