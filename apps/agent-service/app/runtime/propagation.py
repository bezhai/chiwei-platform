"""Trace / lane context propagation primitive (Gap 11).

The runtime moves messages across processes via RabbitMQ. Every transport hop
reads inbound headers into contextvars before invoking business code, and
writes contextvars back into outbound headers before publishing.

Four primitives:

* ``extract_context(headers)`` — defensive parse of inbound headers.
* ``inject_context(headers, ctx=None)`` — write outbound headers (defaults to
  reading current contextvars).
* ``outbound_context(fallback_lane=None)`` — the Context a *producer* should
  inject, with the lane resolved the same way ``mq.publish`` picks the queue.
* ``bind_context(ctx)`` — async context manager that sets contextvars on enter,
  restores on exit (works on success and exception paths).

Business code MUST NOT touch ``trace_id_var`` / ``lane_var`` directly. New
``Source`` types and new transport paths inside ``runtime/`` use these
primitives only — duplicated header-reading logic in durable / debounce /
source-mq / sink-dispatch is replaced by them.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextvars import Token
from dataclasses import dataclass
from typing import Any

from app.api.middleware import lane_var, trace_id_var


@dataclass(frozen=True)
class Context:
    """Propagation context. Both fields may be ``None`` (no value)."""

    trace_id: str | None
    lane: str | None


def _coerce(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def extract_context(headers: dict[str, Any] | None) -> Context:
    """Parse inbound headers (e.g. RabbitMQ message.headers) into a Context.

    Defensive: non-string or empty values become ``None``. Mirrors the manual
    coercion that durable / debounce / source-mq used to do inline.
    """
    h = headers or {}
    return Context(
        trace_id=_coerce(h.get("trace_id")),
        lane=_coerce(h.get("lane")),
    )


def inject_context(
    headers: dict[str, Any] | None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Return ``headers`` augmented with trace_id / lane.

    ``ctx`` defaults to current contextvars. Empty values are written as ``""``
    (not omitted) to match the existing on-wire format consumed by durable /
    debounce / source-mq handlers.
    """
    if ctx is None:
        ctx = Context(trace_id=trace_id_var.get(), lane=lane_var.get())
    out: dict[str, Any] = dict(headers) if headers else {}
    out["trace_id"] = ctx.trace_id or ""
    out["lane"] = ctx.lane or ""
    return out


def outbound_context(*, fallback_lane: str | None = None) -> Context:
    """Build the Context for an outbound publish.

    Lane resolution order: contextvar → ``fallback_lane`` (a lane the Data
    itself carries, used by sink dispatch) → ``current_lane()``, which is the
    ``LANE`` env of the running Deployment. That last step is the whole point:
    ``mq.publish`` picks the queue with ``current_lane()``, so a producer that
    injected the bare contextvar would stamp ``lane: ""`` on a message it just
    routed to ``xxx_ppe-x``. Callers MUST feed ``ctx.lane`` back into
    ``mq.publish(lane=...)`` so the two can never drift apart again.

    The env fallback is producer-only. Consumers stay header-only on purpose:
    a prod pod also drains messages that TTL'd back from a lane queue, so
    reading its own ``LANE`` there would relabel foreign messages.
    """
    from app.infra.rabbitmq import current_lane

    return Context(
        trace_id=trace_id_var.get(),
        lane=lane_var.get() or fallback_lane or current_lane(),
    )


@contextlib.asynccontextmanager
async def bind_context(ctx: Context) -> AsyncIterator[None]:
    """Set contextvars for the duration of the block, restore on exit.

    Both fields are set unconditionally (``None`` clears the var). The reset
    runs on both success and exception paths.
    """
    t_tok: Token[str | None] = trace_id_var.set(ctx.trace_id)
    l_tok: Token[str | None] = lane_var.set(ctx.lane)
    try:
        yield
    finally:
        trace_id_var.reset(t_tok)
        lane_var.reset(l_tok)
