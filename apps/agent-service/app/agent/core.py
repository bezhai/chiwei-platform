"""Unified Agent — single entry point for all LLM interactions.

Every ``run()`` / ``stream()`` / ``extract()`` drives a hand-written ReAct loop
over a neutral :class:`~app.agent.client.ModelClient` (resolved from a model_id
by ``build_model_client``). There is no langchain / langgraph: the loop owns the
control flow, the adapter owns the provider wire, and ``dispatch`` runs tools.

  - ``run``     — loop ``model.complete`` until the assistant stops calling
                  tools; return the final neutral ``Message``.
  - ``stream``  — forward neutral ``StreamChunk``s; on a tool-call turn, dispatch
                  the tools and feed the results back, looping for more turns.
  - ``extract`` — one structured call: ``model.structured`` → dict →
                  ``response_model.model_validate``.

Tracing埋 lives here (the langchain ``CallbackHandler`` is gone): each
run/stream/extract opens one root span via langfuse; the adapter opens a
generation span per LLM call (nested automatically), and every tool dispatch
opens a tool span. ``update_trace`` controls only whether the root *trace*'s
name / IO is overwritten (``update_current_trace``) — the spans are always
produced (guard / deep_research want spans without clobbering the parent trace).

Retry is the Agent layer's sole responsibility (the adapters disable SDK retry).
``run`` / ``extract`` wrap the whole call in ``@retry``; ``stream`` retries only
*before* the first token is yielded (replaying a streamed prefix would duplicate
it downstream).

Usage::

    from app.agent.core import Agent, AgentConfig
    from app.agent.neutral import Message, Role

    CFG = AgentConfig("world_engine", "offline-model", "world-engine")

    result = await Agent(CFG).run(messages=[Message(role=Role.USER, content="hi")])
    text = result.text()

    async for chunk in Agent(CFG, tools=ALL_TOOLS).stream(messages=[...]):
        ...

    data = await Agent(CFG).extract(Model, messages=[...])
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from langfuse import Langfuse
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from app.agent.client import ModelClient, build_model_client
from app.agent.context import AgentContext
from app.agent.neutral import (
    ContentBlock,
    Message,
    Role,
    StreamChunk,
    ToolDef,
    ToolResult,
)
from app.agent.prompts import compile_to_messages, get_prompt
from app.agent.runtime_context import agent_context
from app.agent.session import append_session, load_session
from app.agent.tooling import Tool, dispatch
from app.agent.trace import (
    TURN_TRACE_NAME,
    current_generation_context,
    current_turn_trace_id,
)
from app.capabilities.retry import retry as _retry_decorator
from app.infra import cst_time
from app.infra.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

# TODO(C3): once LLM/HTTP capability layer translates upstream openai errors
# into typed CapabilityError subclasses, switch this tuple to the typed
# defaults exported by ``app.capabilities.retry``.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    RateLimitError,
)

_DEFAULT_MAX_RETRIES = 2
_BACKOFF_BASE = 2  # seconds (exponential base)
_BACKOFF_MAX = 8  # seconds (clamp)

# Max model calls per run/stream — the hand-written loop counts model calls
# directly. The legacy LangGraph ``recursion_limit=12`` counted graph
# super-steps (model + tool nodes alternating), i.e. ~6 model calls / ~6 tool
# rounds. We keep that model-call budget here so runaway-loop cost / latency
# stays equivalent to the langchain path.
_DEFAULT_RECURSION_LIMIT = 6


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Immutable configuration for an agent.

    Each domain module defines its own config constants.
    Use ``dataclasses.replace(cfg, model_id="...")`` for per-call overrides.
    """

    prompt_id: str
    model_id: str
    trace_name: str | None = None
    recursion_limit: int = _DEFAULT_RECURSION_LIMIT


# ---------------------------------------------------------------------------
# Root trace span
# ---------------------------------------------------------------------------

_trace_client: Langfuse | None = None


def _get_trace_client() -> Langfuse:
    global _trace_client
    if _trace_client is None:
        _trace_client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=settings.langfuse_host,
        )
    return _trace_client


class _NoOpRootSpan:
    def update(self, **_kwargs: Any) -> None:
        pass


def _set_current_trace(
    *, name: str | None = None, input: Any = None, session_id: str | None = None
) -> None:
    """Set the current trace's name / input / session, swallowing langfuse errors.

    ``None`` fields are skipped by the SDK, so passing only ``name`` updates the
    trace name and leaves its input untouched. ``session_id`` is a *trace*
    attribute (the langfuse v3 way to group related traces — it is NOT part of a
    span's ``trace_context``); passing it groups this trace into that session.
    Tracing must never break the call.
    """
    try:
        _get_trace_client().update_current_trace(
            name=name, input=input, session_id=session_id
        )
    except Exception as exc:  # pragma: no cover - tracing must not break the call
        logger.warning("langfuse update_current_trace failed: %s", exc)


@contextmanager
def _safe_current_span(
    span_name: str, input: Any, trace_context: dict[str, Any] | None = None
):
    """Enter a langfuse ``start_as_current_span`` defensively, yield it (or no-op).

    Tracing must never break the wrapped call. Both *creating* the span and
    *entering / exiting* its context manager are guarded: any langfuse / OTel
    failure degrades to a no-op span while the body still runs. The body's own
    exceptions (e.g. a retryable LLM error) are NOT swallowed — they propagate
    so the Agent's retry logic still sees them; only span ``__exit__`` failures
    on the way out are swallowed.

    ``trace_context`` (e.g. ``{"trace_id": ...}``) attaches the span to an
    existing trace; used by ``_root_span`` to fold one turn's guard + main spans
    into one trace. ``None`` lets langfuse start a fresh trace (current behaviour).
    """
    try:
        cm = _get_trace_client().start_as_current_span(
            name=span_name, input=input, trace_context=trace_context
        )
        span = cm.__enter__()
    except Exception as exc:
        logger.warning("langfuse span %s unavailable: %s", span_name, exc)
        yield _NoOpRootSpan()
        return

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
            logger.warning("langfuse span %s teardown failed: %s", span_name, exc)


@contextmanager
def _root_span(
    *,
    name: str | None,
    input: Any,
    update_trace: bool,
    session_id: str | None = None,
):
    """Open the run/stream/extract root span and (optionally) name the trace.

    The langchain ``CallbackHandler`` used to do this. We open one
    ``start_as_current_span`` so the adapter's generation spans and our tool
    spans nest under it (langfuse v3 propagates the current span via OTel
    context). When ``update_trace`` is set, the trace's name / input is
    overwritten with this agent's — guard / deep_research pass ``False`` so the
    parent trace keeps its identity while still getting our spans.

    ``session_id`` (when provided) groups this trace into a langfuse session,
    independently of who owns the trace name/input: a guard span with
    ``update_trace=False`` still tags the session. ``None`` leaves the trace's
    session untouched — the chat path passes nothing and behaves exactly as
    before.

    Tracing must never break the call: every langfuse touch degrades to no-op.

    When opened inside a ``turn_trace`` scope (per-turn @node), the span attaches
    to that turn's langfuse trace_id so this turn's guard + main spans fold into
    one trace; outside a turn it stays None and langfuse starts a fresh trace.
    """
    span_name = name or "agent"
    tid = current_turn_trace_id()
    trace_context = {"trace_id": tid} if tid else None
    with _safe_current_span(span_name, input, trace_context) as span:
        if session_id is not None:
            # Session grouping is orthogonal to the name/input ownership below:
            # set it whenever provided so even a guard (update_trace=False) on a
            # session-bound run tags the trace's session.
            _set_current_trace(session_id=session_id)
        if tid is not None:
            # Inside a turn the guards / main / post-safety are separate root
            # spans on one trace; langfuse would name the whole trace after the
            # last one ingested (post-safety). Every root span writes the SAME
            # unified trace name so the name is stable — this is the trace-level
            # name only, each span keeps its own observation name. Only the main
            # path (``update_trace``) owns the trace input, so the trace top
            # reads the chat turn, not a guard's safety prompt.
            _set_current_trace(
                name=TURN_TRACE_NAME, input=input if update_trace else None
            )
        elif update_trace:
            _set_current_trace(name=span_name, input=input)
        yield span


@contextmanager
def _tool_span(*, name: str, input: Any):
    """Open a span around one tool dispatch; degrade to no-op on langfuse error.

    Re-parents under the model call that requested the tool (its generation span
    has closed by now, but its parent_span_id is still a valid parent), so the
    trace reads model-call → its tools instead of a flat list under the agent.
    """
    with _safe_current_span(
        f"tool.{name}", input, trace_context=current_generation_context()
    ) as span:
        yield span


def _record_tool_output(span: Any, result: ToolResult) -> None:
    """Record a dispatched tool's result on its span (best-effort).

    The loop opens the tool span before dispatch so the call arguments land as
    ``input``; without this the span has no ``output`` and langfuse renders the
    tool result as ``undefined``. Content is reduced to JSON-serialisable form
    (block lists → plain dicts) since langfuse must serialise the span. Guarded:
    a tracing failure must never break the tool loop.
    """
    try:
        content = result.content
        output: Any = (
            [b.to_dict() for b in content]
            if isinstance(content, list)
            else content
        )
        span.update(output=output)
    except Exception as exc:  # pragma: no cover - tracing must not break dispatch
        logger.warning("langfuse tool span output update failed: %s", exc)


# ---------------------------------------------------------------------------
# Hand-written ReAct loops (module-level so they can be de-risked in isolation)
# ---------------------------------------------------------------------------


def _tooldefs(tools: list[Tool]) -> list[ToolDef] | None:
    """Project neutral Tools to the ToolDefs the model sees, or None if empty."""
    return [t.definition for t in tools] if tools else None


def _normalise_tool_result(result: ToolResult) -> ToolResult:
    """Coerce a tool's raw return into wire-safe neutral content.

    Tools return ``str`` (search_web / sandbox_bash), ``dict`` (recall / notes /
    a ``@tool_error`` outcome), or ``list[dict]`` OpenAI-style content blocks
    (read_images / generate_image). The model can only be fed ``str`` or
    ``list[ContentBlock]`` (that's what the adapters wire and what
    ``Message.text()`` flattens), mirroring langchain's ToolNode which
    JSON-serialised dict returns and carried block lists as multimodal content:

      - ``str``                     → kept as-is,
      - ``list`` of block-dicts     → ``list[ContentBlock]`` (multimodal),
      - ``dict`` / anything else    → ``json.dumps`` string.

    Returns a new ToolResult so the streamed ``tool_result`` chunk and the tool
    message fed back both carry the normalised content.
    """
    content = result.content
    if isinstance(content, str):
        return result
    if isinstance(content, list):
        blocks = [
            b if isinstance(b, ContentBlock) else ContentBlock.from_dict(b)
            for b in content
        ]
        return ToolResult(tool_call_id=result.tool_call_id, content=blocks)
    # dict (incl. tool_error outcome) or any other JSON-able value → string
    text = json.dumps(content, ensure_ascii=False, default=str)
    return ToolResult(tool_call_id=result.tool_call_id, content=text)


async def _run_loop(
    model: ModelClient,
    *,
    messages: list[Message],
    tools: list[Tool],
    context: AgentContext | None,
    recursion_limit: int,
    session_id: str | None = None,
    model_kwargs: dict[str, Any] | None = None,
    transcript_sink: list[Message] | None = None,
) -> Message:
    """Drive ``model.complete`` until the assistant stops calling tools.

    Each iteration: call the model with the running transcript + tool defs.
    If the assistant requested tools, dispatch each (under ``agent_context`` so
    tool bodies can read the ambient context), append the assistant turn and one
    tool message per result, and loop. Otherwise return the assistant message.

    ``model_kwargs`` (e.g. ``reasoning_effort`` for the safety guard) are
    forwarded to every model call — dropping them silently changes behaviour.
    ``recursion_limit`` caps the number of model calls so a model that keeps
    asking for tools can't loop forever.

    ``transcript_sink`` (when given) collects every message *this loop produces*
    — each assistant turn (with tool calls) + each tool result message + the
    final assistant reply — in order, so the session store can persist the round
    losslessly (the in-memory ``Message`` objects still carry provider blobs like
    ``ToolCall.signature``). It is left untouched on the stateless path.

    Tool calls within one assistant turn are dispatched *sequentially* (langgraph
    ToolNode ran them concurrently). Results are identical; only multi-tool-turn
    latency differs. Kept sequential deliberately — concurrency here is latency
    tuning, not behaviour, and isn't worth the added complexity for the cutover.
    """
    convo = list(messages)
    tool_defs = _tooldefs(tools)
    extra = model_kwargs or {}
    last: Message | None = None

    for _ in range(max(1, recursion_limit)):
        last = await model.complete(
            convo, tools=tool_defs, **{**extra, "session_id": session_id}
        )
        if not last.tool_calls:
            if transcript_sink is not None:
                transcript_sink.append(last)
            return last

        convo.append(last)
        if transcript_sink is not None:
            transcript_sink.append(last)
        for call in last.tool_calls:
            with _tool_span(name=call.name, input=call.arguments) as span:
                with agent_context(context) if context is not None else _nullctx():
                    result = await dispatch(tools, call)
                normalised = _normalise_tool_result(result)
                _record_tool_output(span, normalised)
            tool_msg = normalised.to_message()
            convo.append(tool_msg)
            if transcript_sink is not None:
                transcript_sink.append(tool_msg)

    # recursion limit hit: return the last assistant message we have.
    return last if last is not None else Message(role=Role.ASSISTANT, content="")


async def _stream_loop(
    model: ModelClient,
    *,
    messages: list[Message],
    tools: list[Tool],
    context: AgentContext | None,
    recursion_limit: int,
    session_id: str | None = None,
    model_kwargs: dict[str, Any] | None = None,
    transcript_sink: list[Message] | None = None,
) -> AsyncIterator[StreamChunk]:
    """Stream neutral chunks; on a tool-call turn, dispatch and loop.

    Each turn streams from the model, forwarding every chunk downstream while
    accumulating the assistant's text + any tool calls. If the turn ended with
    tool calls, dispatch them (under ``agent_context``), emit a ``tool_result``
    chunk per result (so the consumer can count them), feed the assistant turn +
    tool messages back into the transcript, and stream another turn. A turn with
    no tool calls ends the loop.

    ``model_kwargs`` are forwarded to every model call. ``recursion_limit`` caps
    the number of streamed turns.

    ``transcript_sink`` (when given) collects every message this loop produces —
    each assistant tool-call turn + each tool result + the final assistant reply
    (reconstructed from the accumulated text/reasoning, since the no-tool-call
    final turn is never appended to ``convo``) — so the session store can persist
    the round. Untouched on the stateless path.
    """
    convo = list(messages)
    tool_defs = _tooldefs(tools)
    extra = model_kwargs or {}

    for _ in range(max(1, recursion_limit)):
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        turn_calls = []

        async for chunk in model.stream(
            convo, tools=tool_defs, **{**extra, "session_id": session_id}
        ):
            if chunk.text:
                text_parts.append(chunk.text)
            if chunk.reasoning:
                reasoning_parts.append(chunk.reasoning)
            if chunk.tool_call is not None:
                turn_calls.append(chunk.tool_call)
            yield chunk

        if not turn_calls:
            # final assistant turn — never appended to convo, so capture it for
            # the session round explicitly (mirrors _run_loop's final append).
            if transcript_sink is not None:
                transcript_sink.append(
                    Message(
                        role=Role.ASSISTANT,
                        content="".join(text_parts),
                        reasoning_content="".join(reasoning_parts) or None,
                    )
                )
            return

        # rebuild the assistant turn from what we streamed, then dispatch. The
        # reasoning is carried back too (mirroring _run_loop, where the Message
        # returned by model.complete already holds reasoning_content) so the
        # next turn's context doesn't lose the model's thoughts.
        assistant_turn = Message(
            role=Role.ASSISTANT,
            content="".join(text_parts),
            reasoning_content="".join(reasoning_parts) or None,
            tool_calls=list(turn_calls),
        )
        convo.append(assistant_turn)
        if transcript_sink is not None:
            transcript_sink.append(assistant_turn)
        for call in turn_calls:
            with _tool_span(name=call.name, input=call.arguments) as span:
                with agent_context(context) if context is not None else _nullctx():
                    result = await dispatch(tools, call)
                result = _normalise_tool_result(result)
                _record_tool_output(span, result)
            tool_msg = result.to_message()
            convo.append(tool_msg)
            if transcript_sink is not None:
                transcript_sink.append(tool_msg)
            yield StreamChunk(tool_result=result)


@contextmanager
def _nullctx():
    yield None


async def _persist_session(session_id: str, messages: list[Message]) -> None:
    """Write this round back to the session store, swallowing write failures.

    The transcript store is now durable PG (``SessionTranscript``), but this
    write-back stays **best-effort continuity**: it runs only *after* the round's
    model + tool side effects have completed, so a write failure here must never
    escape. An exception out of ``Agent.run`` makes the caller's durable @node
    treat an already-completed round as failed → re-deliver / DLQ a round whose
    effects already happened, and the @node's turn marker never gets written so
    idempotency is defeated. Logging + swallowing keeps the round successful; the
    next round simply cold-starts from PG hard facts (symmetric to
    ``load_session`` returning ``[]`` on a missing row).

    So "durable" means the transcript *survives restarts once written* — not that
    every round is guaranteed persisted. A failed write-back drops *that* round's
    continuity (next round cold-starts), by design; the ``log.warning`` makes the
    drop observable rather than silent.
    """
    try:
        await append_session(session_id, messages)
    except Exception as exc:  # noqa: BLE001 - cache write-back must not fail a round
        logger.warning(
            "agent session %s write-back failed, round kept (cold-start next): %s",
            session_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class Agent:
    """Unified thinking entry point.

    ``run`` / ``stream`` drive a hand-written ReAct loop. Having tools or not is
    just a parameter difference, not a code path difference. ``extract`` is the
    structured-output path (single ``model.structured`` call).
    """

    def __init__(
        self,
        config: AgentConfig,
        *,
        tools: list[Tool] | None = None,
        model_kwargs: dict[str, Any] | None = None,
        update_trace: bool = True,
    ) -> None:
        self._cfg = config
        self._tools = tools or []
        self._model_kwargs = model_kwargs or {}
        self._update_trace = update_trace

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    async def _prepare(
        self, prompt_vars: dict[str, Any]
    ) -> tuple[ModelClient, list[Message]]:
        """Resolve the model client and compile the prompt messages."""
        if not self._cfg.prompt_id:
            raise ValueError(
                f"Agent({self._cfg.trace_name}).run/stream requires a non-empty "
                f"prompt_id. Guard agents (empty prompt_id) should use extract()."
            )
        langfuse_prompt = get_prompt(self._cfg.prompt_id)
        model = await build_model_client(self._cfg.model_id)
        # 全局注入的"现在"显式取 CST（北京时间），不依赖容器时区——naive
        # ``datetime.now()`` 在 TZ 不确定的容器里可能是 UTC，喂给每条 prompt 的
        # currTime 就跟 world/life 的 CST 时刻差 8 小时。这一处修正整条 chat 线。
        now = cst_time.now_cst()
        prompt_messages = compile_to_messages(
            langfuse_prompt,
            currDate=now.strftime("%Y-%m-%d"),
            currTime=now.strftime("%H:%M:%S"),
            **prompt_vars,
        )
        return model, prompt_messages

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def run(
        self,
        messages: list[Message],
        *,
        prompt_vars: dict[str, Any] | None = None,
        context: AgentContext | None = None,
        session_id: str | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> Message:
        """Execute the ReAct loop and return the final assistant ``Message``.

        ``session_id`` (decision 1) turns this into a *stateful* continuation:
        the stored transcript for that id is read from Redis and prepended after
        the system prompt, the run continues from there, and this round's new
        messages are appended back (24h TTL refreshed). ``None`` is the stateless
        status quo — Redis is never touched and behaviour is byte-for-byte as
        before. The id also tags the langfuse session (same id, both jobs —
        decision 3).
        """
        model, prompt_messages = await self._prepare(prompt_vars or {})

        # Read the stored history ONCE (before retry) so a retried attempt
        # replays the same prefix and never double-reads. None → stateless.
        history = await load_session(session_id) if session_id else []
        full_messages = [*prompt_messages, *history, *messages]
        trace_session_id = session_id or (context.session_id if context else None)

        @_retry_decorator(
            attempts=max_retries,
            base_delay_s=float(_BACKOFF_BASE),
            max_delay_s=float(_BACKOFF_MAX),
            retry_on=RETRYABLE_EXCEPTIONS,
        )
        async def _invoke() -> tuple[Message, list[Message]]:
            sink: list[Message] | None = [] if session_id else None
            with _root_span(
                name=self._cfg.trace_name,
                input=[m.to_dict() for m in full_messages],
                update_trace=self._update_trace,
                session_id=trace_session_id,
            ):
                result = await _run_loop(
                    model,
                    messages=full_messages,
                    tools=self._tools,
                    context=context,
                    recursion_limit=self._cfg.recursion_limit,
                    session_id=trace_session_id,
                    model_kwargs=self._model_kwargs,
                    transcript_sink=sink,
                )
                return result, (sink or [])

        result, produced = await _invoke()
        # Append this round only on success (after the loop returns / retries
        # settle), so a transient failure that retries the whole call doesn't
        # leave a half-written turn behind. The session store is a *working
        # cache*: by the time we get here the round's tool side effects
        # (emit/move/state writes) have already happened, so a cache write-back
        # failure must NOT bubble out — that would make a durable node treat an
        # already-completed round as failed and re-deliver / DLQ it. Log and
        # swallow; next round cold-starts from PG hard facts (symmetric to
        # load_session's missing-key → cold start).
        if session_id:
            await _persist_session(session_id, [*messages, *produced])
        return result

    async def stream(
        self,
        messages: list[Message],
        *,
        prompt_vars: dict[str, Any] | None = None,
        context: AgentContext | None = None,
        session_id: str | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream neutral ``StreamChunk``s through the ReAct loop.

        Retry only before the first chunk is yielded: once a token reaches the
        consumer, replaying would duplicate the prefix. Backoff math matches
        ``app.capabilities.retry`` (exponential ``base * 2^(N-1)`` clamped) so
        streaming and non-streaming paths stay consistent.

        ``session_id`` (decision 1) makes the stream a stateful continuation:
        the stored transcript is read once up front and prepended after the
        system prompt; once the stream completes, this round's new messages are
        appended back (24h TTL refreshed). ``None`` is the stateless status quo —
        Redis untouched, behaviour byte-for-byte as before.
        """
        model, prompt_messages = await self._prepare(prompt_vars or {})

        history = await load_session(session_id) if session_id else []
        full_messages = [*prompt_messages, *history, *messages]
        trace_session_id = session_id or (context.session_id if context else None)

        for attempt in range(1, max_retries + 1):
            tokens_yielded = False
            sink: list[Message] | None = [] if session_id else None
            try:
                with _root_span(
                    name=self._cfg.trace_name,
                    input=[m.to_dict() for m in full_messages],
                    update_trace=self._update_trace,
                    session_id=trace_session_id,
                ):
                    async for chunk in _stream_loop(
                        model,
                        messages=full_messages,
                        tools=self._tools,
                        context=context,
                        recursion_limit=self._cfg.recursion_limit,
                        session_id=trace_session_id,
                        model_kwargs=self._model_kwargs,
                        transcript_sink=sink,
                    ):
                        tokens_yielded = True
                        yield chunk
                # Stream finished cleanly — persist this round (success only).
                # Same working-cache rule as run(): a write-back failure here is
                # logged and swallowed, never propagated, so an already-streamed
                # round isn't turned into a failed round by a cache miss.
                if session_id:
                    await _persist_session(session_id, [*messages, *(sink or [])])
                return
            except RETRYABLE_EXCEPTIONS as e:
                if tokens_yielded or attempt >= max_retries:
                    raise
                delay = min(_BACKOFF_BASE**attempt, _BACKOFF_MAX)
                logger.warning(
                    "Agent(%s).stream attempt %d/%d failed (%s: %s); "
                    "retrying in %.2fs",
                    self._cfg.trace_name,
                    attempt,
                    max_retries,
                    type(e).__name__,
                    e,
                    delay,
                )
                await asyncio.sleep(delay)

    async def extract(
        self,
        response_model: type[BaseModel],
        messages: list[Message],
        *,
        prompt_vars: dict[str, Any] | None = None,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> BaseModel:
        """Structured output — return a validated Pydantic model instance.

        One ``model.structured`` call against ``response_model``'s JSON schema;
        the returned dict is validated back into the model. Guard agents (empty
        prompt_id) skip prompt compilation.
        """
        model = await build_model_client(self._cfg.model_id)
        schema = response_model.model_json_schema()

        full_messages = list(messages)
        prompt_id = self._cfg.prompt_id
        if prompt_id:
            langfuse_prompt = get_prompt(prompt_id)
            prompt_messages = compile_to_messages(
                langfuse_prompt, **(prompt_vars or {})
            )
            full_messages = [*prompt_messages, *messages]

        @_retry_decorator(
            attempts=max_retries,
            base_delay_s=float(_BACKOFF_BASE),
            max_delay_s=float(_BACKOFF_MAX),
            retry_on=RETRYABLE_EXCEPTIONS,
        )
        async def _invoke() -> BaseModel:
            with _root_span(
                name=self._cfg.trace_name,
                input=[m.to_dict() for m in full_messages],
                update_trace=self._update_trace,
            ):
                data = await model.structured(
                    full_messages, schema=schema, **self._model_kwargs
                )
                return response_model.model_validate(data)

        return await _invoke()
