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

The span is also where **one call's usage is observed**, three ways from the
same numbers: the per-round accumulator (``collect_usage``, landed in durable
PG), langfuse's own usage fields, and Prometheus (``record_llm_usage``). The
last two run on every call, including the degraded path — a langfuse outage must
not take the token accounting with it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

import opentelemetry.trace as _otel_trace
from langfuse import Langfuse
from opentelemetry import context as otel_context

from app.agent.tools._common import get_or_create_counter
from app.infra.config import settings
from app.runtime.lane_policy import current_deployment_lane

if TYPE_CHECKING:
    from collections.abc import Iterator

    from langfuse.model import PromptClient
    from opentelemetry.trace import SpanContext

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
# consumers (e.g. life wake) deliberately do NOT — they are aggregations,
# not a turn, and must stay separate traces. A debounce-propagated runtime
# trace_id would have leaked them into a turn trace, which is exactly why we
# don't seed from the runtime trace_id.
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
# 最近一次模型调用：工具 span 挂在发起它的那次模型调用下面
# ---------------------------------------------------------------------------

# ReAct 循环派发工具的时候，要这些工具的那次模型调用的 generation span 已经结束了，工具 span
# 只按当前上下文开的话会平铺在 agent 根下面。这里记下 generation 的 SpanContext，循环开工具
# span 时把它设成普通的 OTel 父节点（结束了的 span 照样能当父节点），trace 读起来就是"模型
# 调用 → 它要的工具"。
#
# 不能借 langfuse 的 ``trace_context`` 指定这个父节点：SDK 把带 ``trace_context`` 开的 span
# 一律标成 trace 的根（``langfuse.internal.as_root``），服务端拿最后收到的那个根覆盖 trace 的
# 名字、输入输出和时间戳，带工具调用的 trace 就都改叫最后一个工具的名字了。
_last_generation: ContextVar[SpanContext | None] = ContextVar(
    "agent_last_generation", default=None
)


@contextmanager
def under_last_generation() -> Iterator[None]:
    """在这个作用域里开的 span 挂在这个任务最近一次模型调用下面。

    这个任务里还没有过模型调用时什么都不改，span 照常挂在当前 span 下面。"最近一次模型调用"
    由 ``generation_span`` 顺手记下，只覆盖、不恢复：下一次模型调用覆盖它，任务结束它也就
    没了；不恢复也免得跨着异步生成器的 yield 去 reset 一个 ContextVar。
    """
    generation = _last_generation.get()
    if generation is None:
        yield
        return
    token = otel_context.attach(
        _otel_trace.set_span_in_context(_otel_trace.NonRecordingSpan(generation))
    )
    try:
        yield
    finally:
        otel_context.detach(token)


# ---------------------------------------------------------------------------
# 这次调用渲染用的 prompt：每个 generation 关联它实际用的那个 prompt 版本
# ---------------------------------------------------------------------------

# generation 是模型适配层开的，适配层只拿到编译好的消息，拿不到 prompt 对象。prompt 跟"最近
# 一次模型调用"一样放在 tracing 的上下文里带到 ``generation_span``，模型客户端的接口不用认识
# langfuse 的 prompt 对象。
#
# 但它不能像"最近一次模型调用"那样只覆盖、不恢复：外层 agent 的工具里调了另一个 agent，回来
# 之后外层的下一次模型调用要关联的还是外层的 prompt。所以它按作用域设（``rendered_from``）。
_current_prompt: ContextVar[PromptClient | None] = ContextVar(
    "agent_current_prompt", default=None
)


@contextmanager
def rendered_from(prompt: PromptClient | None) -> Iterator[None]:
    """这个作用域里开的 generation 都关联 ``prompt``（它的名字和版本）。

    一次 run / stream / extract 带的是它这次实际编译的那个 prompt 对象：泳道 label 命中就是那个
    版本，回落到 production 就是 production 的版本。``None`` 表示这次调用不是由 prompt 渲染的，
    它会清掉从外层继承来的值，而不是沿用外层的。SDK 自己的兜底 prompt（``is_fallback``）由
    SDK 跳过不关联。

    离开时（包括出异常、被取消、流式调用被消费方关掉）恢复外层的值。恢复用的是"把外层的值设
    回去"，不是 ``ContextVar.reset(token)``：流式调用在异步生成器里进这个作用域，消费方没关就
    丢下的生成器由垃圾回收在另一个上下文里收尾，在那里 reset 会抛 ValueError；设回外层的值
    只落在那个用完就丢的上下文里。
    """
    outer = _current_prompt.get()
    _current_prompt.set(prompt)
    try:
        yield
    finally:
        _current_prompt.set(outer)


# ---------------------------------------------------------------------------
# 另起一条 trace：一个 agent 在工具里调另一个 agent 时用
# ---------------------------------------------------------------------------


@contextmanager
def separate_trace() -> Iterator[None]:
    """在这个作用域里开的 ``Agent`` 根 span 另起一条 langfuse trace。

    一个 agent 在自己的工具里调另一个 agent 时，里面那个的根 span 默认是外面那条 trace 里
    当前工具 span 的子 span，而且会把外面那条 trace 的名字、输入改成自己的。包上这一层：
    当前 OTel 上下文换成空的（里面开的第一个 span 就是一条新 trace 的根），"最近一次模型
    调用"、渲染用的 prompt 和这一轮对话的 trace 都清空。离开时四样都恢复，外面那一轮接下来
    的工具 span 照常挂在它自己的模型调用下面，模型调用照常关联它自己的 prompt。
    """
    otel_token = otel_context.attach(otel_context.Context())
    generation_token = _last_generation.set(None)
    prompt_token = _current_prompt.set(None)
    turn_token = _turn_trace_seed.set(None)
    try:
        yield
    finally:
        _turn_trace_seed.reset(turn_token)
        _current_prompt.reset(prompt_token)
        _last_generation.reset(generation_token)
        otel_context.detach(otel_token)


# ---------------------------------------------------------------------------
# Per-run token usage accumulator:截下一轮 Agent.run 的 token 用量，落 durable PG
# ---------------------------------------------------------------------------

# Token usage 现在只经 ``span.update(usage_details=...)`` 喂给 langfuse，Agent.run
# 不经手。但 langfuse 是 best-effort、会系统性丢 durable 工具的 trace（实测：akao
# 的 act 在 PG 全在、langfuse 名下 0 条），基于它的成本统计严重失真。这个累加器让
# 调用方（world / life 收口）把"本轮 token"截下来落 durable PG —— **真相在 PG**。
#
# 唯一汇聚点：complete / stream / structured 三处 adapter 调用都经过 ``_SafeSpan``
# / ``_NoOpSpan`` 的 ``update``。在那两个 update 里把 usage_details 累加进这个
# contextvar，adapter / Agent.run 签名一行都不用动。
#
# **关键**：langfuse 不可用时走 ``_NoOpSpan``，那时也累加 —— token 来自 LLM
# response，跟 langfuse 死活无关。这正是"不依赖会丢的 langfuse"的意义。
_usage_collector: ContextVar[dict[str, int] | None] = ContextVar(
    "agent_usage_collector", default=None
)


def _accumulate_usage(usage_details: dict[str, Any] | None) -> None:
    """把一次 LLM 调用的 ``usage_details`` 累加进当前 collector（没设就安全跳过）。

    累加维度：input / output / total / cache_read_input_tokens，外加 ``calls``
    （每条带 usage_details 的 update 计一次 model 调用）。只记录、不做任何阈值 /
    控制（赤尾设计宪法：这是观测层）。collector 未设置（不在 ``collect_usage``
    作用域内）时静默跳过——绝大多数 LLM 调用（chat / guard / extract）不收成本，
    只有 world / life 收口才包 collector。
    """
    collector = _usage_collector.get()
    if collector is None or usage_details is None:
        return
    collector["input"] += int(usage_details.get("input", 0) or 0)
    collector["output"] += int(usage_details.get("output", 0) or 0)
    collector["total"] += int(usage_details.get("total", 0) or 0)
    collector["cache_read_input_tokens"] += int(
        usage_details.get("cache_read_input_tokens", 0) or 0
    )
    collector["calls"] += 1


# ---------------------------------------------------------------------------
# Per-call usage metrics: what one LLM call cost, on Prometheus
# ---------------------------------------------------------------------------
#
# The same numbers the accumulator above collects, recorded per call instead of
# per round, and for EVERY call — chat / guard / extract never enter
# ``collect_usage``, so this has to sit where all of them pass: the generation
# span's ``update``, both the langfuse one and the degraded one.
#
# Two metrics rather than one, because "how many tokens" and "was there a number
# at all" are different questions. A provider that stops reporting cache usage
# and a provider reporting a miss both add 0 to the token counter, and dividing
# two token counters gives a ratio that hides how many calls had no cache data
# to begin with. ``llm_usage_reports_total`` is that denominator, split three
# ways per kind: ``nonzero`` / ``zero`` / ``absent``.

# usage_details key → the ``kind`` label it is counted under. ``total`` is left
# out on purpose: it is the provider's own sum and counting it next to its parts
# would double the tokens on any dashboard that sums the metric.
_USAGE_KINDS: dict[str, str] = {
    "input": "input",
    "output": "output",
    "cache_read_input_tokens": "cached",
    "thinking_tokens": "thinking",
}

LLM_TOKENS = get_or_create_counter(
    "llm_tokens_total",
    "Tokens one LLM call was billed for, by kind "
    "(input / output / cached slice of input / thinking)",
    ["lane", "model", "kind"],
)

LLM_USAGE_REPORTS = get_or_create_counter(
    "llm_usage_reports_total",
    "LLM calls by what the provider reported for each usage kind: "
    "nonzero / zero / absent (the provider did not report the field)",
    ["lane", "model", "kind", "report"],
)


def _metrics_lane() -> str:
    """The lane label, always explicit — prod is written out, never left blank."""
    return current_deployment_lane() or "prod"


def record_llm_usage(model: str, usage_details: dict[str, Any] | None) -> None:
    """Record one LLM call's usage. Called once per call, right where it lands.

    ``usage_details`` is what the adapter read off the response; ``None`` means
    the provider reported no usage at all, and every kind is then counted as
    ``absent`` — a call that cost something unmeasured is still a call.

    Observation only: nothing here decides anything (赤尾设计宪法 — this is the
    observability layer), and a metrics failure must never break a call that has
    already succeeded.
    """
    lane = _metrics_lane()
    try:
        for key, kind in _USAGE_KINDS.items():
            raw = (usage_details or {}).get(key)
            if raw is None:
                report = "absent"
            elif int(raw):
                report = "nonzero"
                LLM_TOKENS.labels(lane=lane, model=model, kind=kind).inc(int(raw))
            else:
                report = "zero"
            LLM_USAGE_REPORTS.labels(
                lane=lane, model=model, kind=kind, report=report
            ).inc()
    except Exception as exc:  # pragma: no cover - metrics must not break the call
        logger.warning("llm usage metrics failed: %s", exc)


@contextmanager
def collect_usage() -> Iterator[dict[str, int]]:
    """累计本作用域内所有 LLM 调用的 token 用量，yield 一个零初值累加 dict。

    world / life 收口把 ``Agent.run`` 包在这个 contextmanager 里，run 完读 yield
    出来的 dict 拿本轮累计 token 落 durable PG。退出时 reset（成功 / 异常都 reset），
    不让累加器跨轮泄漏。``calls`` 是本轮 LLM 调用次数（工具循环可能多轮 model 调用）。
    """
    accumulator: dict[str, int] = {
        "input": 0,
        "output": 0,
        "total": 0,
        "cache_read_input_tokens": 0,
        "calls": 0,
    }
    token = _usage_collector.set(accumulator)
    try:
        yield accumulator
    finally:
        _usage_collector.reset(token)


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


class _UsageOnce:
    """Takes one call's usage down exactly once, whatever ``update`` is called.

    A streamed call folds many chunks into one span and reports the running
    total on the last ``update``; a caller updating twice with usage would then
    bill the same call twice. The first update carrying a ``usage_details``
    keyword is the measurement — later ones are ignored.
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self._taken = False

    def take(self, kwargs: dict[str, Any]) -> None:
        if self._taken or "usage_details" not in kwargs:
            return
        self._taken = True
        usage = kwargs.get("usage_details")
        _accumulate_usage(usage)
        record_llm_usage(self._model, usage)


class _NoOpSpan:
    """A generation span that does nothing — used when langfuse is unavailable."""

    def __init__(self, model: str) -> None:
        # langfuse 死了也要记本次调用的用量：它来自 LLM response，与 langfuse 无关。
        # 这正是"不依赖会丢的 langfuse"做成本观测的意义。
        self._usage = _UsageOnce(model)

    def update(self, **kwargs: Any) -> None:
        self._usage.take(kwargs)

    def end(self, **_kwargs: Any) -> None:
        pass


class _SafeSpan:
    """Wraps a langfuse generation so ``update`` / ``end`` never raise.

    The LLM response is already in hand by the time callers record output /
    usage, so a langfuse serialisation or transport error there must NOT fail
    the (successful) LLM call. Every delegated call is swallowed and logged.
    """

    def __init__(self, generation: Any, model: str) -> None:
        self._gen = generation
        self._usage = _UsageOnce(model)

    def update(self, **kwargs: Any) -> None:
        # 先记本次调用的用量（独立于 langfuse 死活），再喂 langfuse。即使下面
        # langfuse update 抛了，用量也已经入账——成本观测不被 langfuse 失败拖累。
        self._usage.take(kwargs)
        try:
            self._gen.update(**kwargs)
        except Exception as exc:
            logger.warning("langfuse generation update failed: %s", exc)

    def end(self, **kwargs: Any) -> None:
        try:
            self._gen.end(**kwargs)
        except Exception as exc:
            logger.warning("langfuse generation end failed: %s", exc)


def mark_failed(span: Any, exc: BaseException) -> None:
    """把这个 span 记成失败（level ERROR），带上为什么；``exc`` 不算失败时什么都不做。

    OTel 只给 ``Exception`` 设错误状态，``CancelledError`` 是 ``BaseException``：被外面的期限
    掐断的那次调用（moment 的 900 秒占用就是这么结束卡住的那次调用的）不留任何错误，在 Langfuse
    里是 DEFAULT、没有输出，跟一次没出事的调用看不出区别。所以这里直接写 Langfuse 的 level 和
    status message，不靠服务端从 OTel 状态推断。

    ``GeneratorExit`` 不算失败：那是流式调用的消费方不再往下拉（拿到 content_filter 就收手），
    这次调用该交的都交了。

    记不上就算了：tracing 不能把调用本身搞坏。
    """
    if not isinstance(exc, (Exception, asyncio.CancelledError)):
        return
    reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    try:
        span.update(level="ERROR", status_message=reason)
    except Exception as err:  # pragma: no cover - tracing must not break the call
        logger.warning("langfuse span failure mark failed: %s", err)


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
    so a failed call still produces a (truncated) span rather than vanishing,
    marked as failed with the reason (``mark_failed``).

    A langfuse failure (unconfigured keys, network) degrades to a no-op span;
    the wrapped LLM call always proceeds.

    Opened *as the current span* so anything nested during the call hangs under
    it; its span context is kept for the tools this call asks for, which the loop
    dispatches after it has closed (``under_last_generation``).

    关联的 prompt 是这次调用所在的 ``rendered_from`` 作用域里那一个。
    """
    try:
        cm = _get_client().start_as_current_generation(
            name=name,
            model=model,
            input=input,
            model_parameters=model_parameters,
            metadata=metadata,
            prompt=_current_prompt.get(),
        )
        gen = cm.__enter__()
    except Exception as exc:
        logger.warning("langfuse generation span unavailable: %s", exc)
        yield _NoOpSpan(model)
        return

    # 记下这次模型调用：循环随后派发它要的工具时，工具 span 挂在它下面（这时它已经结束）。
    generation = _otel_trace.get_current_span().get_span_context()
    _last_generation.set(generation if generation.is_valid else None)

    span = _SafeSpan(gen, model)
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
            logger.warning("langfuse generation span teardown failed: %s", exc)
