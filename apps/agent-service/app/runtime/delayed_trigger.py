"""Runtime-owned delayed trigger queue (Gap 9.1.2 / 9.3).

Architecture:

* ``DelayedTriggerEnvelope`` wraps ``(origin_app, origin_lane,
  data_type, payload, trace_id)`` and is published with x-delay to
  ``runtime_delayed_trigger_{origin_app}`` (lane queue handled by
  mq.publish via lane_queue helper).
* Each runtime instance declares + consumes the route for its OWN
  ``APP_NAME`` only. Cross-app envelopes are guarded by
  ``origin_app`` validation.
* When the envelope's delay expires, ``_runtime_trigger_consumer``
  rebuilds the original Data and calls ``emit(data)`` under the
  envelope's trace/lane context — preserving full fan-out semantics
  (in-process consumers in own_nodes run directly, cross-process
  consumers see standard mq publish).
* The trigger wire is registered by ``register_runtime_trigger_wire``
  during runtime startup (after migrate_schema, before
  compile_graph). It is NOT module-level so tests can opt in
  selectively without polluting WIRING_REGISTRY.
"""

from __future__ import annotations

import importlib
import logging
import os
import uuid
from typing import Annotated, Any

from pydantic import Field

from app.infra.rabbitmq import (
    KNOWN_APPS_FOR_DELAYED_TRIGGER,
    trigger_route_for,
)
from app.runtime.data import Data, Key
from app.runtime.emit import emit
from app.runtime.node import node
from app.runtime.propagation import Context, bind_context
from app.runtime.source import Source
from app.runtime.wire import wire

logger = logging.getLogger(__name__)


def trigger_route_name_for(app: str) -> str:
    """Convenience accessor — returns the queue name for ``app``."""
    return f"runtime_delayed_trigger_{app}"


def _new_envelope_id() -> str:
    return uuid.uuid4().hex


class DelayedTriggerEnvelope(Data):
    """Framework-internal Data wrapping a delayed emit() request.

    Not a business Data — declared via the same Data primitive so the
    runtime's mq source loop can decode it the same way as any other
    Data type. The payload field carries the original Data's JSON dump.

    ``envelope_id`` is a runtime-generated UUID acting as the Key (Data
    contract requires at least one). It also doubles as the
    runtime_inflight idempotent key on the consumer side, ensuring two
    separate emit_delayed calls never share dedup state.
    """

    envelope_id: Annotated[str, Key] = Field(default_factory=_new_envelope_id)
    origin_app: str
    origin_lane: str | None = None
    data_type: str           # f"{cls.__module__}.{cls.__qualname__}"
    payload: dict[str, Any] = {}
    trace_id: str | None = None


def _resolve_data_class(data_type: str) -> type[Data] | None:
    """Look up a Data subclass by ``module.qualname``. None if not found."""
    try:
        module_path, _, qualname = data_type.rpartition(".")
        if not module_path:
            return None
        mod = importlib.import_module(module_path)
        cls = getattr(mod, qualname, None)
        if cls is None or not isinstance(cls, type) or not issubclass(cls, Data):
            return None
        return cls
    except Exception:
        logger.exception("failed resolving data_type=%s", data_type)
        return None


def current_app() -> str:
    """Return APP_NAME env, defaulting to 'agent-service'."""
    return os.getenv("APP_NAME") or "agent-service"


@node
async def _runtime_trigger_consumer(envelope: DelayedTriggerEnvelope) -> None:
    """Internal consumer: validate origin_app, rebuild data, call emit().

    Defensive validation:
    - origin_app != APP_NAME → log error + ack (envelope mis-routed; the
      lane_fallback=False guard on trigger queues should make this
      unreachable, but we don't want a silent wrong-process emit).
    - data_type unresolvable → log warning + ack (Data class deleted
      between publish and consume).
    - payload validation error → log + ack (malformed envelope).

    On success, emit() runs under the envelope's trace/lane so the
    delayed dispatch joins the originating Langfuse trace instead of
    surfacing as an independent root.
    """
    app = current_app()
    if envelope.origin_app != app:
        logger.error(
            "delayed trigger envelope origin_app=%s does not match "
            "APP_NAME=%s; dropping (queue routing failure)",
            envelope.origin_app,
            app,
        )
        return
    cls = _resolve_data_class(envelope.data_type)
    if cls is None:
        logger.warning(
            "delayed trigger envelope data_type=%s not found; dropping",
            envelope.data_type,
        )
        return
    try:
        data = cls(**envelope.payload)
    except Exception:
        logger.exception(
            "delayed trigger envelope payload failed validation: data_type=%s",
            envelope.data_type,
        )
        return
    ctx = Context(trace_id=envelope.trace_id, lane=envelope.origin_lane)
    async with bind_context(ctx):
        await emit(data)


def register_runtime_trigger_wire(app: str) -> None:
    """Register the trigger Source.mq → consumer wire for ``app``.

    Called from ``bootstrap.prepare_for_run`` after load_dataflow_graph
    and before any consumer freezes a WIRING_REGISTRY snapshot.
    Runtime-internal — business code must NOT call this.

    Idempotent: if a wire for ``DelayedTriggerEnvelope`` with the same
    queue is already in ``WIRING_REGISTRY`` we no-op. This lets
    ``Runtime.run`` keep its legacy fallback call (some test fixtures
    drive Runtime directly without going through bootstrap) without
    double-registering when bootstrap already ran.

    ``app`` must be in KNOWN_APPS_FOR_DELAYED_TRIGGER (rabbitmq.py); a
    fresh app needs its trigger queue route declared in ALL_ROUTES
    first. We re-validate here so a typo in APP_NAME crashes loudly at
    startup instead of silently failing to consume envelopes.
    """
    if app not in KNOWN_APPS_FOR_DELAYED_TRIGGER:
        raise ValueError(
            f"register_runtime_trigger_wire: app={app!r} not in "
            f"KNOWN_APPS_FOR_DELAYED_TRIGGER ({KNOWN_APPS_FOR_DELAYED_TRIGGER}); "
            f"register the trigger route in app/infra/rabbitmq.py first."
        )
    queue = trigger_route_for(app).queue

    # Idempotency check: a wire whose data_type is DelayedTriggerEnvelope
    # AND whose sources include an mq source on the same queue already
    # exists in the registry.
    from app.runtime.wire import WIRING_REGISTRY

    for w in WIRING_REGISTRY:
        if w.data_type is not DelayedTriggerEnvelope:
            continue
        for s in w.sources:
            if s.kind == "mq" and s.params.get("queue") == queue:
                logger.debug(
                    "runtime_delayed_trigger wire already registered for "
                    "app=%s queue=%s; skipping",
                    app,
                    queue,
                )
                return

    wire(DelayedTriggerEnvelope).from_(Source.mq(queue)).to(
        _runtime_trigger_consumer
    )
    logger.info(
        "runtime_delayed_trigger wire registered: app=%s queue=%s",
        app,
        queue,
    )
