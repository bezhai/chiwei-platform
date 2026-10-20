"""Unified Agent — single entry point for all LLM interactions.

``run()`` drives a hand-written ReAct loop over a neutral
:class:`~app.agent.client.ModelClient` (resolved from a model_id by
``build_model_client``); ``extract()`` makes one structured call. There is no
langchain / langgraph: the loop owns the control flow, the adapter owns the
provider wire, and ``dispatch`` runs tools.

  - ``run``     — loop ``model.complete`` until the assistant stops calling
                  tools, or calls one its declaration says ends the round
                  (``ToolSpec.ends_round``); return the final neutral ``Message``.
  - ``extract`` — one structured call: ``model.structured`` → dict →
                  ``response_model.model_validate``.

Tracing埋 lives here (the langchain ``CallbackHandler`` is gone): each
run/extract opens one root span via langfuse and records what it hands
back as that span's output; the adapter opens a generation span per LLM call
(nested automatically), and every tool dispatch opens a tool span under the
generation that requested it. ``update_trace`` controls only whether the root
*trace*'s name / IO is overwritten (``update_current_trace``) — the spans are
always produced (the output check wants spans without clobbering the parent
trace).

Retry is the Agent layer's sole responsibility (the adapters disable SDK retry).
``run`` / ``extract`` wrap the whole call in ``@retry``. A model call whose
provider never answers fails at ``MODEL_ANSWER_SECONDS`` (``app.agent.client``)
with ``CapabilityTimeout``, which is retried like the provider's own timeouts.

Usage::

    from app.agent.contracts import AgentSpec, Segment
    from app.agent.core import Agent
    from app.agent.neutral import Message, Role
    from app.agent.prompts import PromptSet
    from app.agent.round import open_call

    SPEC = AgentSpec(segments=("world_engine",), model="offline-model", trace_name="world-engine")

    prompts = await PromptSet.resolve(
        {"world_engine": SPEC}, {"world_engine": Segment("world_engine", "world_engine")}, None
    )
    call = open_call("world_engine", SPEC, lane="prod")
    result = await Agent(SPEC).run(
        [Message(role=Role.USER, content="hi")], call=call, prompts=prompts
    )
    text = result.text()

The caller opens the call and keeps it (:mod:`app.agent.round`): the agent binds it while it
runs, and what the call's tools noted is read from it afterwards. The prompt comes from the
set the caller resolved (:class:`app.agent.prompts.PromptSet`): a participant's round resolves
it once for itself and the sub-agents it may call; a call outside any round resolves its own.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping, Sequence
from contextlib import contextmanager, nullcontext
from typing import Any

from langfuse import Langfuse
from langfuse.model import PromptClient
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from app.agent.client import ModelClient, build_model_client
from app.agent.contracts import AgentSpec, FromConfig, ToolSpec
from app.agent.neutral import (
    ContentBlock,
    Message,
    Role,
    ToolCall,
    ToolDef,
    ToolResult,
    TurnPartKind,
)
from app.agent.prompts import PromptSet
from app.agent.round import Call, in_call
from app.agent.tooling import Tool, dispatch
from app.agent.trace import (
    mark_failed,
    rendered_from,
    separate_trace,
    under_last_generation,
)
from app.api.middleware import get_lane
from app.capabilities._errors import CapabilityTimeout
from app.capabilities.retry import retry as _retry_decorator
from app.deployment import DEFAULT_APP
from app.infra.config import settings
from app.infra.lane_policy import (
    classify_deployment_lane,
    current_deployment_lane,
    normalize_deployment_lane,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Retry configuration
# ---------------------------------------------------------------------------

# TODO(C3): once LLM/HTTP capability layer translates upstream openai errors
# into typed CapabilityError subclasses, switch this tuple to the typed
# defaults exported by ``app.capabilities.retry``.
#
# ``CapabilityTimeout`` is what a model call raises when its provider never
# answers within ``MODEL_ANSWER_SECONDS`` (``app.agent.client``). It is a
# timeout like ``APITimeoutError`` and gets the same treatment: retried up to the
# caller's ``max_retries``. Callers that must not replay a whole run pass
# ``max_retries=1`` and get the failure straight away.
RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = (
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
    RateLimitError,
    CapabilityTimeout,
)

_BACKOFF_BASE = 2  # seconds (exponential base)
_BACKOFF_MAX = 8  # seconds (clamp)

# Max attempts (including the first) for ONE ReAct turn when the model returns
# an empty result — no text, no tool_calls (trace 82323210372fe067ec2a60abd8e9
# fdb3: a gemini-3.5-flash generate_image closure turn came back as
# ``{"text": "", "tool_calls": []}``, silently passed through as the final
# reply). 3 gives the model two independent extra samples to recover from what
# looks like a decoding glitch rather than a deterministic refusal, while
# staying inside the "2-3 attempts" a one-off empty turn should reasonably
# cost. This budget is spent entirely INSIDE one turn (same transcript, no new
# messages, no tool re-dispatch) and is independent of ``recursion_limit``
# (which counts ReAct turns, not raw model calls) — the normal, non-empty path
# still costs exactly one model call, so this never adds latency/cost to the
# common case.
_EMPTY_TURN_MAX_ATTEMPTS = 3


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


def _trace_attributes() -> tuple[dict[str, str], list[str]]:
    """Return Langfuse trace attributes that make app and lane lookup cheap.

    ``tags`` are the primary query surface (``list-traces`` supports tags
    directly); metadata carries the same fields for the trace detail page and
    later analytics. The app is the one this process runs as (``APP_NAME``,
    injected by PaaS per Deployment), so a world round is tagged ``app:world``
    and an agent-service run ``app:agent-service``.
    """
    app_name = os.getenv("APP_NAME") or DEFAULT_APP
    raw_request_lane = get_lane()
    if raw_request_lane:
        lane = normalize_deployment_lane(raw_request_lane) or "prod"
        source = "request"
    else:
        deployment_lane = current_deployment_lane()
        lane = deployment_lane or "prod"
        source = "deployment" if deployment_lane else "default"

    lane_class = classify_deployment_lane(lane)
    metadata = {
        "app": app_name,
        "lane": lane,
        "laneClass": lane_class,
        "laneSource": source,
    }
    tags = [
        f"app:{app_name}",
        f"lane:{lane}",
        f"lane_class:{lane_class}",
    ]
    return metadata, tags


def _set_current_trace(
    *, name: str | None = None, input: Any = None, session_id: str | None = None
) -> None:
    """Set current trace attributes, swallowing langfuse errors.

    ``None`` fields are skipped by the SDK, so passing only ``name`` updates the
    trace name and leaves its input untouched. ``session_id`` is a *trace*
    attribute (the langfuse v3 way to group related traces — it is NOT part of a
    span's ``trace_context``); passing it groups this trace into that session.
    Lane tags/metadata are always written so every root trace is queryable by
    ``tags=["lane:<lane>"]`` even when ``update_trace=False``. Tracing must never
    break the call.
    """
    try:
        metadata, tags = _trace_attributes()
        _get_trace_client().update_current_trace(
            name=name,
            input=input,
            session_id=session_id,
            metadata=metadata,
            tags=tags,
        )
    except Exception as exc:  # pragma: no cover - tracing must not break the call
        logger.warning("langfuse update_current_trace failed: %s", exc)


@contextmanager
def _safe_current_span(span_name: str, input: Any):
    """Enter a langfuse ``start_as_current_span`` defensively, yield it (or no-op).

    Tracing must never break the wrapped call. Both *creating* the span and
    *entering / exiting* its context manager are guarded: any langfuse / OTel
    failure degrades to a no-op span while the body still runs. The body's own
    exceptions (e.g. a retryable LLM error) are NOT swallowed — they propagate
    so the Agent's retry logic still sees them, after the span is marked as
    failed with the reason (``mark_failed``); only span ``__exit__`` failures on
    the way out are swallowed.
    """
    try:
        cm = _get_trace_client().start_as_current_span(name=span_name, input=input)
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
        mark_failed(span, exc)
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
    prompt: PromptClient | None = None,
):
    """Open the run/extract root span and (optionally) name the trace.

    The langchain ``CallbackHandler`` used to do this. We open one
    ``start_as_current_span`` so the adapter's generation spans and our tool
    spans nest under it (langfuse v3 propagates the current span via OTel
    context). When ``update_trace`` is set, the trace's name / input is
    overwritten with this agent's — the output check passes ``False`` so the
    parent trace keeps its identity while still getting our spans.

    调用方把这次调用交回去的结果记成这个 span 的 output（``_record_output``）。它是 trace 的根
    时，langfuse 拿它的名字、输入输出和开始时间当 trace 的。

    ``prompt`` 是这次调用实际编译的那个 prompt 对象，这个 span 里开的每个 generation 都关联它
    （``rendered_from``）；``None`` 是不由 prompt 渲染的调用，清掉外层调用设的那个。离开时恢复
    外层的，外层 agent 的工具里调完这一次，外层接下来的模型调用照旧关联外层的 prompt。

    ``session_id`` (when provided) groups this trace into a langfuse session,
    independently of who owns the trace name/input: a guard span with
    ``update_trace=False`` still tags the session. ``None`` leaves the trace's
    session untouched — the chat path passes nothing and behaves exactly as
    before.

    Tracing must never break the call: every langfuse touch degrades to no-op.
    """
    span_name = name or "agent"
    with (
        _safe_current_span(span_name, input) as span,
        rendered_from(prompt),
    ):
        _set_current_trace(
            name=span_name if update_trace else None,
            input=input if update_trace else None,
            session_id=session_id,
        )
        yield span


@contextmanager
def _tool_span(*, name: str, input: Any):
    """Open a span around one tool dispatch; degrade to no-op on langfuse error.

    挂在要这个工具的那次模型调用下面（那个 generation 这时已经结束，但仍能当父节点），trace
    读起来是"模型调用 → 它要的工具"，而不是平铺在 agent 根下面。父节点走普通的 OTel 上下文
    （``under_last_generation``），所以工具 span 不会被 langfuse 当成 trace 的根。
    """
    with under_last_generation(), _safe_current_span(f"tool.{name}", input) as span:
        yield span


def _record_segments(span: Any, prompts: PromptSet, agent: str) -> None:
    """Record on the call's root span which segments it rendered, at which label and version
    (best-effort). An agent rendered from several segments links no one prompt on its model
    calls, so this is where its versions are read back."""
    try:
        span.update(
            metadata={
                "prompt_segments": [
                    {"prompt_id": prompt_id, "label": label, "version": version}
                    for prompt_id, label, version in prompts.versions(agent)
                ]
            }
        )
    except Exception as exc:  # pragma: no cover - tracing must not break the call
        logger.warning("langfuse span metadata update failed: %s", exc)


def _record_output(span: Any, output: Any) -> None:
    """Record what a span's call handed back as its ``output`` (best-effort).

    The output must already be JSON-serialisable (langfuse serialises the span).
    Guarded: a tracing failure must never break the call it records.
    """
    try:
        span.update(output=output)
    except Exception as exc:  # pragma: no cover - tracing must not break the call
        logger.warning("langfuse span output update failed: %s", exc)


def _record_tool_output(span: Any, result: ToolResult) -> None:
    """Record a dispatched tool's result on its span.

    The loop opens the tool span before dispatch so the call arguments land as
    ``input``; without this the span has no ``output`` and langfuse renders the
    tool result as ``undefined``. Content is reduced to JSON-serialisable form
    (block lists → plain dicts) since langfuse must serialise the span.
    """
    content = result.content
    _record_output(
        span, [b.to_dict() for b in content] if isinstance(content, list) else content
    )


# ---------------------------------------------------------------------------
# Hand-written ReAct loops (module-level so they can be de-risked in isolation)
# ---------------------------------------------------------------------------

# What a call that never ran is answered with. A tool that ends the round
# (``ToolSpec.ends_round``; hers is ``stop_for_now``) ends the run at
# the position it holds in the turn, so every call after it is left unexecuted —
# and an assistant turn whose calls are not all answered makes the provider
# reject the whole next request. The wording says what happened, so a model
# reading the stored context does not take it for a tool that failed.
NEVER_RAN = "（这一轮在这只手之前就结束了，它没有执行。）"

def _tooldefs(tools: list[Tool]) -> list[ToolDef] | None:
    """Project neutral Tools to the ToolDefs the model sees, or None if empty."""
    return [t.definition for t in tools] if tools else None


def _normalise_tool_result(result: ToolResult) -> ToolResult:
    """Coerce a tool's raw return into wire-safe neutral content.

    Tools return ``str`` (search_web / run_a_script), ``dict`` (recall / notes /
    a ``@tool_error`` outcome), or ``list[dict]`` OpenAI-style content blocks
    (any tool handing back pictures). The model can only be fed ``str`` or
    ``list[ContentBlock]`` (that's what the adapters wire and what
    ``Message.text()`` flattens), mirroring langchain's ToolNode which
    JSON-serialised dict returns and carried block lists as multimodal content:

      - ``str``                     → kept as-is,
      - ``list`` of block-dicts     → ``list[ContentBlock]`` (multimodal),
      - ``dict`` / anything else    → ``json.dumps`` string.

    Returns a new ToolResult so the tool message fed back carries the
    normalised content.
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


def _carries_something(message: Message) -> bool:
    """Whether this message still says anything once it is replayed.

    A message with no tool calls, no turn sequence and no content reaches the
    provider as an empty turn — the gemini adapter builds ``parts=[]`` for it
    and the request is rejected before the model sees it. A turn that only
    thought does carry something: the adapter replays its thought parts, and
    their signatures are what the model continues its reasoning from.
    """
    if message.tool_calls or message.turn_parts:
        return True
    content = message.content
    return bool(content.strip()) if isinstance(content, str) else bool(content)


def _is_empty_turn(text: str, tool_calls: list[ToolCall]) -> bool:
    """Whether one model turn produced nothing usable: no tool_calls AND no
    non-whitespace text.

    ``text`` should be the turn's plain-text view (``Message.text()``) —
    thinking is deliberately excluded: a turn that only thought (thought parts
    on the turn) without producing text or a tool_call is still empty *as an
    answer*, because thinking is never surfaced to the user. This is a different question from
    ``_carries_something`` (whether the turn is worth replaying), which counts
    thoughts because the provider gets them back.
    """
    return not tool_calls and not text.strip()


def _model_label(model: ModelClient) -> str:
    """Best-effort model identifier for the empty-turn warning log.

    ``ModelClient`` has no public "which model" accessor; both real adapters
    (openai, gemini) stash the resolved model name on the private ``_model``
    attribute. Falling back to the class name keeps this purely diagnostic —
    never load-bearing — so a fake/test client or a future adapter without
    that attribute still logs something useful instead of raising.
    """
    return getattr(model, "_model", None) or type(model).__name__


async def _complete_turn(
    model: ModelClient,
    convo: list[Message],
    tool_defs: list[ToolDef] | None,
    call_kwargs: dict[str, Any],
) -> list[Message]:
    """Call ``model.complete`` for one ReAct turn and return every turn the
    model produced for it, in order — the last one is this turn's result.

    A turn that answered nothing (see ``_is_empty_turn``) is retried, up to
    ``_EMPTY_TURN_MAX_ATTEMPTS`` times, without dispatching tools or appending
    anything else to the conversation. But an attempt that answered nothing may
    still have *thought*, and those thoughts carry the signatures the model
    continues its reasoning from — so the attempt is not thrown away: it is
    appended to the transcript the retry is issued against, and handed back here
    for the loop to store. Two different questions, kept apart: "did this turn
    produce an answer" (retry, and ``_is_empty_turn`` stays blind to thinking on
    purpose, so a turn that only thought is never handed to her as a reply) and
    "is this turn worth replaying" (``_carries_something`` — an attempt that
    came back with nothing at all is dropped, since an empty turn on the wire
    makes the provider reject the next request).

    Exhausting the budget changes nothing about the result or control flow below
    this call — the caller gets whatever the last attempt produced (possibly
    still empty), exactly as it always has. Only a warning log marks the
    exhaustion, so this never introduces a new exception type or return shape:
    raising here would let the exception escape into a caller's broad
    ``except Exception``, which would turn it into user-facing error text —
    exactly the "send something anyway" outcome this fix exists to prevent.
    """
    working = list(convo)
    kept: list[Message] = []
    attempts = 0
    while True:
        attempts += 1
        last = await model.complete(working, tools=tool_defs, **call_kwargs)
        if not _is_empty_turn(last.text(), last.tool_calls):
            break
        if attempts >= _EMPTY_TURN_MAX_ATTEMPTS:
            logger.warning(
                "agent turn empty (no text, no tool_calls) after %d/%d attempts; "
                "model=%s",
                attempts,
                _EMPTY_TURN_MAX_ATTEMPTS,
                _model_label(model),
            )
            break
        if _carries_something(last):
            kept.append(last)
            working.append(last)
    return [*kept, last]


async def _run_loop(
    model: ModelClient,
    *,
    messages: list[Message],
    tools: list[Tool],
    recursion_limit: int,
    ends_round: frozenset[str] = frozenset(),
    session_id: str | None = None,
    model_kwargs: dict[str, Any] | None = None,
    transcript_sink: list[Message] | None = None,
) -> Message:
    """Drive ``model.complete`` until the assistant stops calling tools.

    Each iteration: call the model with the running transcript + tool defs.
    If the assistant requested tools, dispatch each (a tool reads its call and
    round with ``current_call()`` / ``current_round()``, bound by the agent and
    the round's runner), append the assistant turn and one tool message per
    result, and loop. Otherwise return the assistant message.

    ``model_kwargs`` (e.g. ``reasoning_effort`` for the safety guard) are
    forwarded to every model call — dropping them silently changes behaviour.
    ``recursion_limit`` caps the number of tool-bearing model calls so a model
    that keeps asking for tools can't loop forever. Spending that budget does not
    cut the run off mid-thought: the loop makes one more call **without tools**,
    so the last turn's tool results reach the model and the run returns what the
    assistant actually says.

    ``transcript_sink`` (when given) collects every message *this loop produces*
    — each assistant turn (the ones with tool calls, and the ones that only
    thought before being retried) + each tool result message + the final
    assistant reply — in order, so a caller keeping a continuous context
    can store the round losslessly (the in-memory ``Message`` objects still carry
    provider blobs like ``ToolCall.signature``).

    Two rules keep what lands there replayable, because a caller feeds it back
    as the next round's history and the provider validates it:

      - **every call is answered.** A terminal tool ends the run where it sits
        in the turn, so the calls after it never run; each of those gets a
        result saying so (``NEVER_RAN``).
      - **nothing content-free is stored** (``_carries_something``). The empty
        message a terminal tool returns stays out — it carries nothing, and a
        caller whose rounds usually end that way would pile up one per round —
        and so does a closing turn left with nothing after its unanswerable
        call is stripped.

    Tool calls within one assistant turn are dispatched *sequentially* (langgraph
    ToolNode ran them concurrently). Results are identical; only multi-tool-turn
    latency differs. Kept sequential deliberately — concurrency here is latency
    tuning, not behaviour, and isn't worth the added complexity for the cutover.
    """
    convo = list(messages)
    tool_defs = _tooldefs(tools)
    call_kwargs = {**(model_kwargs or {}), "session_id": session_id}
    last: Message | None = None

    for _ in range(max(1, recursion_limit)):
        # An answerless attempt that thought comes back in front of the turn it
        # was retried into; it is part of this round's transcript like any other
        # model turn, and the next request replays its signatures.
        *unanswered, last = await _complete_turn(
            model, convo, tool_defs, call_kwargs
        )
        for turn in unanswered:
            convo.append(turn)
            if transcript_sink is not None:
                transcript_sink.append(turn)
        if not last.tool_calls:
            if transcript_sink is not None:
                transcript_sink.append(last)
            return last

        convo.append(last)
        if transcript_sink is not None:
            transcript_sink.append(last)
        for at, call in enumerate(last.tool_calls):
            with _tool_span(name=call.name, input=call.arguments) as span:
                result = await dispatch(tools, call)
                normalised = _normalise_tool_result(result)
                _record_tool_output(span, normalised)
            tool_msg = normalised.to_message()
            convo.append(tool_msg)
            if transcript_sink is not None:
                transcript_sink.append(tool_msg)
            if call.name in ends_round:
                # The calls after this one never ran, and the assistant turn
                # carrying them is already stored. Answer each so the stored
                # round keeps every call paired with a result — a turn with an
                # unanswered call makes the provider reject the next request.
                for skipped in last.tool_calls[at + 1 :]:
                    cancelled = ToolResult(
                        tool_call_id=skipped.id, content=NEVER_RAN
                    ).to_message()
                    convo.append(cancelled)
                    if transcript_sink is not None:
                        transcript_sink.append(cancelled)
                # The empty message is the run's *return value* only — it stays
                # out of the sink. It carries no text and no tool call, so a
                # caller that stores the sink as its continuous context would
                # accumulate one empty message per terminated round.
                return Message(role=Role.ASSISTANT, content="")

    # Budget spent. Hand the dispatched tool results back one last time with no
    # tools, so the run ends on what the assistant says rather than on a
    # tool-call turn whose ``text()`` is usually empty: that message read as
    # "what it said" is an empty string stored with no error and no log, and the
    # last turn's tool results would never reach the model at all.
    logger.warning(
        "agent spent its budget of %d model calls with tools still pending; "
        "closing with one toolless call; model=%s",
        recursion_limit,
        _model_label(model),
    )
    *unanswered, closing = await _complete_turn(model, convo, None, call_kwargs)
    for turn in unanswered:
        if transcript_sink is not None:
            transcript_sink.append(turn)
    if closing.tool_calls:
        # A model handed an empty tool list can still ask for a tool. Keeping
        # that call would leave the stored context with a call nothing answers,
        # and the provider rejects the whole next request over it. The rest of
        # the turn stays — its thoughts and their signatures are what the next
        # request replays — minus the parts pointing at the dropped calls.
        closing = Message(
            role=Role.ASSISTANT,
            content=closing.content,
            turn_parts=[
                p
                for p in closing.turn_parts
                if p.kind is not TurnPartKind.TOOL_CALL
            ],
        )
    # Stripping the call can leave a turn with nothing on it — the closing call
    # came back with no text and only that call. Storing it would put an empty
    # turn in the context, which the provider rejects on the next request just
    # as surely as the unanswered call would have. It is still the run's return
    # value; only the stored round drops it.
    if transcript_sink is not None and _carries_something(closing):
        transcript_sink.append(closing)
    return closing


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


async def _configured(value: str | int | FromConfig) -> Any:
    """``value`` itself, or what it reads from Dynamic Config now."""
    return await value.read() if isinstance(value, FromConfig) else value


class Agent:
    """Unified thinking entry point for one agent (its :class:`AgentSpec`).

    ``run`` drives a hand-written ReAct loop. Having tools or not is just a
    parameter difference, not a code path difference. ``extract`` is the
    structured-output path (single ``model.structured`` call). The tools are
    given as their declarations (:class:`~app.agent.contracts.ToolSpec`): the
    model sees their schemas in the order given, and a call to one declared
    ``ends_round`` ends the run.

    Every entry takes the :class:`~app.agent.round.Call` its caller opened and binds it as the
    current call for as long as it runs; the call's own state is emptied at the start of each
    attempt. Its prompt comes from the :class:`~app.agent.prompts.PromptSet` resolved for the
    round (or for the call) under the call's name; ``values`` are the variables known only at the
    call. The root span records the segments rendered (``prompt_segments``); the model calls
    link the prompt when the agent has a single one. The model and the budget of model calls are read when the call starts (they may come
    from Dynamic Config); a whole call is tried ``spec.max_retries`` times on a temporary model
    failure. ``spec.separate_trace`` puts every call in a Langfuse trace of its own.
    """

    def __init__(self, spec: AgentSpec, *, tools: Sequence[ToolSpec] = ()) -> None:
        self._spec = spec
        self._tools = [t.tool for t in tools]
        self._ends_round = frozenset(t.tool.name for t in tools if t.ends_round)

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _trace_scope(self):
        return separate_trace() if self._spec.separate_trace else nullcontext()

    async def _model(self) -> tuple[ModelClient, int]:
        """The model client and the budget of model calls, read when the call starts."""
        model = await build_model_client(await _configured(self._spec.model))
        return model, await _configured(self._spec.recursion_limit)

    def _retry(self):
        return _retry_decorator(
            attempts=self._spec.max_retries,
            base_delay_s=float(_BACKOFF_BASE),
            max_delay_s=float(_BACKOFF_MAX),
            retry_on=RETRYABLE_EXCEPTIONS,
        )

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def run(
        self,
        messages: list[Message],
        *,
        call: Call,
        prompts: PromptSet,
        values: Mapping[str, str] | None = None,
        transcript_sink: list[Message] | None = None,
    ) -> Message:
        """Execute the ReAct loop and return the final assistant ``Message``.

        ``transcript_sink`` (when given) is extended with every message this run
        produced — each assistant turn with its tool calls, each tool result, and
        the final reply, in order — so a caller that keeps a continuous context
        can store the round losslessly. The Agent itself reads and writes no
        store: a caller that continues a conversation passes the prior messages in
        ``messages`` and decides when and with what else the round is persisted
        (:mod:`app.agent.continuity`).

        Only the messages of a *successful* attempt reach the sink: a retried
        attempt starts a fresh collection, so a transient failure never leaves a
        half-written turn behind. The call's own state starts empty on every
        attempt for the same reason.
        """
        spec = self._spec
        with in_call(call), self._trace_scope():
            model, recursion_limit = await self._model()
            full_messages = [*prompts.messages(call.agent, values), *messages]

            @self._retry()
            async def _invoke() -> tuple[Message, list[Message]]:
                call.clear_state()
                sink: list[Message] = []
                with _root_span(
                    name=spec.trace_name,
                    input=[m.to_dict() for m in full_messages],
                    update_trace=spec.update_trace,
                    session_id=call.session,
                    prompt=prompts.single_prompt(call.agent),
                ) as span:
                    _record_segments(span, prompts, call.agent)
                    result = await _run_loop(
                        model,
                        messages=full_messages,
                        tools=self._tools,
                        ends_round=self._ends_round,
                        recursion_limit=recursion_limit,
                        session_id=call.session,
                        model_kwargs=dict(spec.model_kwargs),
                        transcript_sink=sink,
                    )
                    _record_output(span, result.to_dict())
                    return result, sink

            result, produced = await _invoke()
        if transcript_sink is not None:
            transcript_sink.extend(produced)
        return result

    async def extract(
        self,
        response_model: type[BaseModel],
        messages: list[Message],
        *,
        call: Call,
        prompts: PromptSet,
        values: Mapping[str, str] | None = None,
    ) -> BaseModel:
        """Structured output — return a validated Pydantic model instance.

        One ``model.structured`` call against ``response_model``'s JSON schema;
        the returned dict is validated back into the model.

        The call's session (when its agent has one) tags this trace's langfuse
        session; ``extract`` stays stateless either way (no transcript
        read/write): a session is a trace tag only, not a continuation.
        """
        spec = self._spec
        with in_call(call), self._trace_scope():
            model = await build_model_client(await _configured(spec.model))
            schema = response_model.model_json_schema()
            full_messages = [*prompts.messages(call.agent, values), *messages]

            @self._retry()
            async def _invoke() -> BaseModel:
                call.clear_state()
                with _root_span(
                    name=spec.trace_name,
                    input=[m.to_dict() for m in full_messages],
                    update_trace=spec.update_trace,
                    session_id=call.session,
                    prompt=prompts.single_prompt(call.agent),
                ) as span:
                    _record_segments(span, prompts, call.agent)
                    data = await model.structured(
                        full_messages, schema=schema, **dict(spec.model_kwargs)
                    )
                    result = response_model.model_validate(data)
                    _record_output(span, result.model_dump(mode="json"))
                    return result

            return await _invoke()
