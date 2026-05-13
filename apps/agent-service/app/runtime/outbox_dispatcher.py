"""Phase 7b Gap 8: outbox dispatcher loop.

The dispatcher's ONLY job is to call ``emit(data)`` once per pending
runtime_outbox row, then mark the row dispatched. It does NOT publish
to RabbitMQ directly — emit() owns the wire fan-out (in-process /
durable / debounce / sink). At-least-once: if emit() succeeds and the
DB UPDATE crashes mid-flight, the next loop will pick the row up
again. Consumer-side runtime_inflight dedup absorbs the repeat for
durable wires; in-process wires must be side-effect-free or
self-idempotent (see spec §4.7 + §4.5.2).

Filter: SELECT WHERE origin_app = APP_NAME AND lane IS NOT DISTINCT
FROM current_lane() — prod and dev-lane pods do not race for the same
row.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
from typing import Any

from sqlalchemy import text

from app.api.middleware import lane_var, trace_id_var
from app.data.session import get_session
from app.infra.rabbitmq import current_lane
from app.runtime.data import Data
from app.runtime.emit import emit
from app.runtime.placement import DEFAULT_APP

logger = logging.getLogger(__name__)


def _current_app() -> str:
    return os.getenv("APP_NAME") or DEFAULT_APP


def deserialize_data(data_type: str, payload_json: dict[str, Any]) -> Data:
    """Resolve a fully-qualified ``module.Class`` name to its Data subclass
    and reconstruct an instance from the JSON payload.
    """
    mod_name, _, cls_name = data_type.rpartition(".")
    if not mod_name:
        raise RuntimeError(f"invalid data_type {data_type!r}")
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name)
    if not issubclass(cls, Data):
        raise RuntimeError(f"{data_type!r} resolved to non-Data class")
    return cls(**payload_json)


class _Bound:
    """Async context manager: bind trace_id + lane vars from a row."""

    def __init__(self, *, trace_id: str | None, lane: str | None) -> None:
        self._t_token = None
        self._l_token = None
        self._tid = trace_id
        self._lane = lane

    async def __aenter__(self):
        self._t_token = trace_id_var.set(self._tid)
        self._l_token = lane_var.set(self._lane)
        return self

    async def __aexit__(self, *exc):
        lane_var.reset(self._l_token)
        trace_id_var.reset(self._t_token)


def bind_propagation_from_payload(*, trace_id: str | None,
                                  lane: str | None) -> _Bound:
    return _Bound(trace_id=trace_id, lane=lane)


async def _drain_once(*, app: str, lane: str | None,
                      batch_size: int = 32) -> int:
    """One pass of the loop. Returns number of rows touched."""
    async with get_session() as s:
        rows = (await s.execute(text(
            "SELECT id, data_type, payload_json, lane, trace_id, origin_app "
            "FROM runtime_outbox "
            "WHERE state = 'pending' "
            "  AND next_attempt_at <= now() "
            "  AND origin_app = :app "
            "  AND lane IS NOT DISTINCT FROM :lane "
            "ORDER BY id "
            "LIMIT :n "
            "FOR UPDATE SKIP LOCKED"
        ), {"n": batch_size, "app": app, "lane": lane})).mappings().all()

        for row in rows:
            try:
                async with bind_propagation_from_payload(
                    trace_id=row["trace_id"], lane=row["lane"],
                ):
                    data = deserialize_data(row["data_type"], row["payload_json"])
                    await emit(data)
                await s.execute(text(
                    "UPDATE runtime_outbox "
                    "SET state='dispatched', dispatched_at=now() "
                    "WHERE id=:i"
                ), {"i": row["id"]})
            except Exception as exc:
                # Classification: PER-ROW routed (contract §4.4). Dispatcher
                # loop must keep draining the next outbox row even when one
                # emit() crashes—we record the failure on the row (attempts++
                # with exponential backoff in next_attempt_at) so the next pass
                # retries; ack semantics happen at row level via state column,
                # not via broker. Killing the dispatcher loop here would stall
                # every other pending row in the table.
                logger.exception(
                    "outbox dispatch failed id=%s data_type=%s",
                    row["id"], row["data_type"],
                )
                await s.execute(text(
                    "UPDATE runtime_outbox "
                    "SET attempts = attempts + 1, "
                    "    last_error = :e, "
                    "    next_attempt_at = now() + (interval '5 seconds' * power(2, attempts)) "
                    "WHERE id=:i"
                ), {"i": row["id"], "e": str(exc)[:500]})
        await s.commit()
        return len(rows)


async def dispatcher_loop(*, batch_size: int = 32, idle_sleep_ms: int = 200) -> None:
    """Long-running loop. Cancel the task to stop."""
    app = _current_app()
    lane = current_lane()
    logger.info("outbox dispatcher started app=%s lane=%s", app, lane)
    try:
        while True:
            n = await _drain_once(app=app, lane=lane, batch_size=batch_size)
            if n == 0:
                await asyncio.sleep(idle_sleep_ms / 1000)
    except asyncio.CancelledError:
        logger.info("outbox dispatcher stopping")
        raise
