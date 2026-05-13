"""emit(): publish a Data instance into the compiled dataflow graph.

Looks up every wire whose ``data_type`` matches the emitted instance and,
for each wire, applies the optional ``when()`` predicate and dispatches
to the consumers. In-process edges call the consumer directly (awaiting
completion); ``durable()`` edges hand off to the durable queue layer.

In-process dispatch is strict: if any consumer raises, the remaining
fan-out (sibling consumers and later-matching wires) is aborted and the
exception propagates to ``emit``'s caller. Use ``.durable()`` when
independent isolation between consumers is required.

``.fan_out_per(extractor)`` (B7) is the one in-process exception to the
"strict propagation" rule: each per-key consumer call is awaited via
``asyncio.gather(return_exceptions=True)``, so one key's raise is
logged and the other keys still run. This is the contract that lets
business code drop hand-rolled ``for pid in pids: try: ...`` loops.

Persistence: a wire declared ``.as_latest()`` makes ``emit`` append a
new versioned row to the Data class's table before dispatching. This is
what lets a downstream ``with_latest(X)`` consumer (or any out-of-graph
``query()`` reader) actually find the row. The append happens once per
``emit`` even when the same Data class has multiple wires — what gets
persisted is the Data instance, not a per-wire copy.

``with_latest(X)`` inputs are resolved by fetching the latest ``X`` row
whose first Key field matches the same-named attribute on the emitted
data. ``select_latest`` returning ``None`` is treated as a wiring bug
and raised — a consumer that declared ``with_latest(X)`` cannot run
without an ``X`` to join against.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from datetime import UTC

from app.runtime.data import Data
from app.runtime.graph import CompiledGraph, compile_graph
from app.runtime.placement import DEFAULT_APP, nodes_for_app

logger = logging.getLogger(__name__)

_graph: CompiledGraph | None = None


def reset_emit_runtime() -> None:
    global _graph
    _graph = None


def _get_graph() -> CompiledGraph:
    global _graph
    if _graph is None:
        _graph = compile_graph()
    return _graph


def _current_app() -> str:
    return os.getenv("APP_NAME") or DEFAULT_APP


async def emit(data: Data) -> None:
    graph = _get_graph()
    own_nodes = nodes_for_app(_current_app())
    cls = type(data)

    # Persist before dispatch: any wire(cls).as_latest() declaration
    # means the Data must land in pg first, so downstream with_latest()
    # readers see it. Same instance, one append per emit — multiple
    # as_latest wires on the same class don't multiply the row count.
    if any(w.data_type is cls and w.as_latest for w in graph.wires):
        from app.runtime.persist import insert_append

        await insert_append(data)

    for w in graph.wires:
        if w.data_type is not cls:
            continue
        if w.predicate and not w.predicate(data):
            continue
        if w.debounce is not None:
            # debounce wire 走独立的 mq publish 路径（publish_debounce），
            # 不参与 in-process / sink / durable dispatch；compile_graph
            # 已保证 .debounce() 不能跟 .durable() / .as_latest() /
            # .when() / sink 等组合，这里直接 fan-out 到每个 consumer 后
            # continue。
            from app.runtime.debounce import publish_debounce

            for c in w.consumers:
                await publish_debounce(w, c, data)
            continue
        if w.fan_out_extractor is not None:
            # B7: per-key fan-out with failure isolation. Each per-key
            # consumer call is awaited concurrently with
            # ``return_exceptions=True`` so one persona's failure does
            # not abort the others. compile_graph 已校验 fan_out_per 不
            # 跟 durable / debounce / with_latest 组合，所以这里走纯
            # in-process 分发，sinks 仍按非 fan-out 路径走。
            await _dispatch_fan_out(w, data, own_nodes)
            for s in w.sinks:
                if s.kind == "mq":
                    from app.runtime.sink_dispatch import _dispatch_mq_sink

                    await _dispatch_mq_sink(s, data)
            continue
        for c in w.consumers:
            if w.durable:
                # durable: publish to the consumer's queue; the bound
                # worker will consume and run it. No app-side filter.
                from app.runtime.durable import publish_durable

                await publish_durable(w, c, data)
                continue

            if c in own_nodes:
                # in-process: consumer is bound to (or falls through to)
                # THIS process's app — call directly.
                kwargs = await _resolve_inputs(c, data, w)
                await c(**kwargs)
                continue

            # Consumer is in another process. If the wire has Source.mq,
            # publish to that queue so the worker pod consumes it.
            # Otherwise raise — A0 contract W4a: cross-app dispatch
            # without an explicit transport (`.durable()` / Source.mq) is
            # banned because it silently drops the Data on the floor.
            # Surfaces the wiring bug at the first emit instead of letting
            # downstream logic mysteriously never run.
            mq_src = next((s for s in w.sources if s.kind == "mq"), None)
            if mq_src is not None:
                await _mq_publish_for_source(mq_src, data)
                continue
            raise RuntimeError(
                f"wire({cls.__name__}).to({c.__name__}): cross-app dispatch "
                f"has no transport — add .durable() so emit publishes to "
                f"the consumer's queue, or add .from_(Source.mq(...)) so "
                f"an external producer can reach the consumer. Current "
                f"emit-side app is {_current_app()!r}; consumer is bound "
                f"elsewhere."
            )
        # Phase 2: sink dispatch — out-of-graph publish (RabbitMQ).
        # compile_graph 已校验 Sink.mq(name) ∈ ALL_ROUTES，这里直接调。
        for s in w.sinks:
            if s.kind == "mq":
                from app.runtime.sink_dispatch import _dispatch_mq_sink

                await _dispatch_mq_sink(s, data)


async def _dispatch_fan_out(wire_spec, data: Data, own_nodes) -> None:
    """B7: fan_out_per dispatch with per-key isolation.

    Calls ``wire_spec.fan_out_extractor()`` (sync or async) to get a
    ``list[dict]``; for each item builds ``data.model_copy(update=item)``
    and invokes every consumer in parallel via ``asyncio.gather`` with
    ``return_exceptions=True``. Per-key failures are logged but do NOT
    abort the rest — this is the contract that lets business code drop
    hand-rolled ``for pid in pids: try: ...`` loops.

    Extractor failure (e.g. DB jitter on persona listing) is also
    swallowed and logged: same fail-soft semantic as
    ``_fan_out_per_persona``'s outer try/except, so a single jittered
    tick doesn't bubble back to the source loop and crash the process.
    """
    extractor = wire_spec.fan_out_extractor
    try:
        result = extractor()
        if inspect.isawaitable(result):
            items = await result
        else:
            items = result
    except Exception:
        logger.exception(
            "fan_out_per extractor failed for wire(%s); dropping this emit",
            type(data).__name__,
        )
        return

    if not items:
        return

    tasks = []
    labels: list[tuple[str, dict]] = []
    for item in items:
        try:
            data_copy = data.model_copy(update=dict(item))
        except Exception:
            logger.exception(
                "fan_out_per could not apply update=%r to %s; skipping this key",
                item,
                type(data).__name__,
            )
            continue
        for c in wire_spec.consumers:
            if wire_spec.durable:
                # compile-time check already rejects this combo; defensive.
                raise RuntimeError(
                    "fan_out_per + durable should have been rejected at compile"
                )
            if c in own_nodes:
                kwargs = await _resolve_inputs(c, data_copy, wire_spec)
                tasks.append(c(**kwargs))
                labels.append((c.__name__, dict(item)))
            else:
                # Cross-process within fan_out_per is unsupported for now.
                # Mirrors emit()'s default-path requirement of an explicit
                # transport (.durable() / Source.mq), but fan_out_per
                # forbids durable. Surface the wiring bug.
                raise RuntimeError(
                    f"wire({type(data).__name__}).fan_out_per().to("
                    f"{c.__name__}): consumer is bound to another app; "
                    f"fan_out_per is in-process only (durable combo is "
                    f"disallowed)."
                )

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for (cname, key), res in zip(labels, results, strict=False):
        if isinstance(res, BaseException):
            logger.warning(
                "fan_out_per consumer %s failed for key=%r on %s: %r",
                cname,
                key,
                type(data).__name__,
                res,
            )


async def _resolve_inputs(consumer, data: Data, wire_spec) -> dict:
    from app.runtime.data import key_fields
    from app.runtime.node import inputs_of
    from app.runtime.persist import select_latest

    ins = inputs_of(consumer)
    kwargs: dict = {}
    for name, t in ins.items():
        if t is type(data):
            kwargs[name] = data
        elif t in wire_spec.with_latest:
            key = key_fields(t)[0]
            val = getattr(data, key, None)
            if val is None:
                raise RuntimeError(
                    f"with_latest({t.__name__}) needs {key} on "
                    f"{type(data).__name__}"
                )
            latest = await select_latest(t, {key: val})
            if latest is None:
                raise RuntimeError(
                    f"with_latest({t.__name__}) found no row for "
                    f"{key}={val!r}; an upstream "
                    f"wire({t.__name__}).as_latest() must have written at "
                    f"least one version before {consumer.__name__} fires"
                )
            kwargs[name] = latest
    return kwargs


async def _mq_publish_for_source(src, data: Data) -> None:
    """Publish ``data`` to the mq queue declared by ``src``.

    Looks up the Route in ALL_ROUTES by queue name (queue must be a known
    route — compile_graph rejects unknown queues at startup). Body is
    ``data.model_dump(mode='json')`` to mirror Source.mq consumer-side
    decoding. Lane resolution happens inside ``mq.publish``.
    """
    from app.infra.rabbitmq import mq
    from app.runtime.sink_dispatch import _route_by_queue

    queue_name = src.params["queue"]
    route = _route_by_queue(queue_name)
    if route is None:
        raise RuntimeError(
            f"emit() cannot publish {type(data).__name__} to "
            f"Source.mq({queue_name!r}): queue is not in ALL_ROUTES. "
            f"Add it to app/infra/rabbitmq.py."
        )
    body = data.model_dump(mode="json")
    await mq.publish(route, body)


# ---------------------------------------------------------------------------
# emit_delayed / emit_at — Phase 7a Gap 9
# ---------------------------------------------------------------------------

# RabbitMQ x-delayed-message exchange uses int32 ms (~24 days). Reject
# anything beyond so the ValueError surfaces at the call site rather than
# silently saturating to broker behavior.
_X_DELAY_MAX_MS = 2_147_483_647


async def emit_delayed(
    data: Data,
    *,
    delay_ms: int,
    durability: str = "durable",
) -> None:
    """Schedule emit(data) to run after ``delay_ms`` milliseconds.

    Semantics: the contract is "delay → emit(data)" — the runtime preserves
    full fan-out at firing time. emit_delayed itself does NOT inspect the
    Data's wire topology; it just defers the standard emit() call.

    durability="durable" (default): publish-with-confirm a
    DelayedTriggerEnvelope to the runtime's per-app trigger queue
    (runtime_delayed_trigger_{APP_NAME} + lane). The runtime's internal
    consumer rebuilds the Data and calls emit(data) when the delay
    expires — survives pod restart / deploy as long as the origin
    app+lane comes back up.

    durability="best_effort": schedule an asyncio task in this process.
    Lost on runtime stop / pod restart / deploy. trace/lane do NOT
    propagate across the scheduled boundary (downstream chain is an
    independent trace, same as cron triggers). Caller MUST opt in
    explicitly.

    Negative delay clamps to 0 (immediate await emit). delay_ms above
    _X_DELAY_MAX_MS raises ValueError (broker x-delay header is int32).
    """
    if durability not in ("durable", "best_effort"):
        raise ValueError(
            f"durability must be 'durable' or 'best_effort', "
            f"got {durability!r}"
        )
    if delay_ms < 0:
        delay_ms = 0
    if delay_ms > _X_DELAY_MAX_MS:
        raise ValueError(
            f"delay_ms={delay_ms} exceeds RabbitMQ x-delay int32 max "
            f"({_X_DELAY_MAX_MS} ms ≈ 24 days)"
        )

    if delay_ms == 0:
        await emit(data)
        return

    if durability == "best_effort":
        from app.runtime.scheduled import schedule_after

        async def _fire() -> None:
            await emit(data)

        await schedule_after(delay_ms / 1000.0, _fire)
        return

    # durable path: trigger queue + envelope
    from app.api.middleware import lane_var, trace_id_var
    from app.infra.rabbitmq import (
        KNOWN_APPS_FOR_DELAYED_TRIGGER,
        mq,
        trigger_route_for,
    )
    from app.runtime.delayed_trigger import DelayedTriggerEnvelope
    from app.runtime.propagation import inject_context

    app = _current_app()
    if app not in KNOWN_APPS_FOR_DELAYED_TRIGGER:
        raise RuntimeError(
            f"emit_delayed(durability='durable') unavailable for "
            f"APP_NAME={app!r}: app is not in "
            f"KNOWN_APPS_FOR_DELAYED_TRIGGER. Either pass "
            f"durability='best_effort' (lost on restart) or register "
            f"the trigger route in app/infra/rabbitmq.py."
        )
    lane = lane_var.get()
    cls = type(data)
    envelope = DelayedTriggerEnvelope(
        origin_app=app,
        origin_lane=lane,
        data_type=f"{cls.__module__}.{cls.__qualname__}",
        payload=data.model_dump(mode="json"),
        trace_id=trace_id_var.get(),
    )
    body = envelope.model_dump(mode="json")
    route = trigger_route_for(app)
    headers = inject_context({"data_type": "DelayedTriggerEnvelope"})
    confirmed = await mq.publish_with_confirm(
        route, body,
        headers=headers,
        delay_ms=delay_ms,
        lane=lane,
    )
    if not confirmed:
        raise RuntimeError(
            f"EmitDelayedDispatchFailed: publish-confirm failed for "
            f"{cls.__name__} (origin_app={app}, lane={lane})"
        )


async def emit_at(
    data: Data,
    *,
    when,  # datetime.datetime
    durability: str = "durable",
) -> None:
    """Emit ``data`` at absolute time ``when`` (UTC if naive).

    Past ``when`` becomes delay_ms=0 (immediate). Otherwise
    ``delay_ms = (when - now).total_seconds() * 1000``, clamped to the
    same x-delay limit as ``emit_delayed``.
    """
    from datetime import datetime

    now = datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    delta = (when - now).total_seconds()
    delay_ms = max(0, int(delta * 1000))
    await emit_delayed(data, delay_ms=delay_ms, durability=durability)
