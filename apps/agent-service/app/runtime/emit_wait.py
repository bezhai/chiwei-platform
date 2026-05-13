"""``emit_and_wait`` — process-local request/reply on the dataflow graph (B1).

Lets a caller emit a request Data, then await a typed reply Data
correlated by a named field, without hand-rolling a global future
registry per use case.

Replaces ``app/chat/pre_safety_gate.py`` (global ``_waiters`` dict +
hand-written ``register``/``resolve``/``cleanup`` + bespoke
``run_pre_safety_via_graph``). The reply side no longer needs a dedicated
``resolve_waiter`` @node: ``emit()`` calls :func:`notify` after each
dispatch, and any matching waiter (same Data type + correlation value)
fires its future.

Scope:
  * **In-process only.** The reply must be emitted in the same process
    that called ``emit_and_wait`` — pre-safety is in-process today.
    Cross-process request/reply would need a durable correlation table;
    not in scope for B1.
  * **Best-effort on pod restart.** If the pod dies while the verdict
    sits on the durable wire, the verdict will eventually re-deliver and
    ``notify()`` will see no waiter (registry is process-local) and do
    nothing — same behaviour as the legacy ``_waiters`` dict.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.runtime.data import Data

logger = logging.getLogger(__name__)


class EmitWaitTimeout(TimeoutError):
    """Raised by :func:`emit_and_wait` when no matching reply arrives in
    ``timeout_s`` seconds. Subclass of ``TimeoutError`` so callers that
    want fail-open can ``except TimeoutError``."""


# Keyed by (reply Data type, correlation value). One waiter per key —
# concurrent waiters with the same key are a wiring bug (e.g. duplicate
# correlation ids).
_waiters: dict[tuple[type[Data], str], asyncio.Future[Data]] = {}


def _register(
    wait_for: type[Data], correlation: str
) -> asyncio.Future[Data]:
    """Allocate a future bound to (wait_for, correlation). Raise if a
    waiter already exists for that key — duplicate correlation ids would
    silently steal each other's replies."""
    key = (wait_for, correlation)
    if key in _waiters:
        raise RuntimeError(
            f"emit_and_wait: duplicate waiter for "
            f"({wait_for.__name__}, correlation={correlation!r}). "
            f"Use a unique correlation id per call."
        )
    fut: asyncio.Future[Data] = asyncio.get_running_loop().create_future()
    _waiters[key] = fut
    return fut


def _unregister(wait_for: type[Data], correlation: str) -> None:
    _waiters.pop((wait_for, correlation), None)


def notify(data: Data) -> None:
    """Called by :func:`app.runtime.emit.emit` after dispatching ``data``.

    If a waiter exists for ``(type(data), data.<correlation_field>)``,
    resolve its future. ``correlation_field`` is implicit: we scan the
    registry for a key whose first slot equals ``type(data)`` and whose
    second slot matches one of ``data``'s attributes. Concretely we only
    need to match by *value*, so we look up by every registered key with
    the same type and check if ``getattr(data, field)`` equals the
    correlation. We avoid hardcoding the field name by storing the
    field name alongside the future.
    """
    # Build the candidate keys: any registered (cls, _) where cls is
    # type(data) or a parent (Data subclasses are concrete — exact match
    # is enough).
    cls = type(data)
    # Fast path: nothing waiting.
    if not _waiters:
        return
    # We need to know which attribute to read off ``data`` for matching.
    # That information lives in ``_field_for[(cls, correlation)]`` — set
    # by ``emit_and_wait`` at register time. Match against every key in
    # the registry whose type equals ``cls``.
    matched_key: tuple[type[Data], str] | None = None
    for (wait_cls, correlation), _fut in _waiters.items():
        if wait_cls is not cls:
            continue
        field = _field_for.get((wait_cls, correlation))
        if field is None:
            continue
        try:
            val = getattr(data, field)
        except AttributeError:
            continue
        if val == correlation:
            matched_key = (wait_cls, correlation)
            break
    if matched_key is None:
        return
    fut = _waiters.get(matched_key)
    if fut is None or fut.done():
        return
    fut.set_result(data)


# Parallel to _waiters: stores the correlation field name per waiter so
# notify() knows which attribute to read off the reply Data.
_field_for: dict[tuple[type[Data], str], str] = {}


async def emit_and_wait(
    data: Data,
    *,
    wait_for: type[Data],
    correlation: str,
    correlation_field: str,
    timeout_s: float,
) -> Data:
    """Emit ``data`` into the graph, then await a reply of type
    ``wait_for`` whose ``correlation_field`` attribute equals
    ``correlation``.

    Concurrency model: ``emit(data)`` is launched as an inner task and
    raced against the waiter future and the timeout. This mirrors the
    legacy ``run_pre_safety_via_graph`` pattern — the reply node may
    auto-emit its return value, which means inside the same emit()
    chain the reply fires synchronously; without an inner task the
    waiter would deadlock waiting on its own emit() to complete first.

    Failure modes:
      * Timeout         -> ``EmitWaitTimeout`` (registry cleaned).
      * emit() raises   -> the original exception (registry cleaned).
      * Caller cancel   -> CancelledError propagates (registry cleaned).
    """
    # Import here to avoid circular import (emit.py imports emit_wait.notify).
    from app.runtime.emit import emit

    key = (wait_for, correlation)
    fut = _register(wait_for, correlation)
    _field_for[key] = correlation_field

    emit_task: asyncio.Task = asyncio.create_task(emit(data))
    completed = False
    try:
        done, _pending = await asyncio.wait(
            {fut, emit_task},
            timeout=timeout_s,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            # Timeout: neither the future nor emit_task completed.
            raise EmitWaitTimeout(
                f"emit_and_wait: no {wait_for.__name__} matching "
                f"{correlation_field}={correlation!r} within {timeout_s}s"
            )
        if fut in done:
            # Got the reply. If emit_task is still running let it finish
            # so we don't leak — but if it raises, log and prefer the
            # successful verdict (mirror legacy behaviour).
            if not emit_task.done():
                with suppress(Exception):
                    await emit_task
            elif emit_task.exception() is not None:
                logger.warning(
                    "emit_and_wait: emit_task raised after reply arrived: %s",
                    emit_task.exception(),
                )
            result = fut.result()
            completed = True
            return result
        # emit_task completed first (without reply). If it raised, surface;
        # otherwise we have to wait the remaining time for the reply (the
        # emit chain may have queued the reply on a durable wire that
        # arrives later — but in our in-process scope that's not the
        # normal path, so fall through to a short final wait).
        assert emit_task in done
        exc = emit_task.exception()
        if exc is not None:
            raise exc
        # emit_task succeeded but no reply yet — race remaining time.
        try:
            result = await asyncio.wait_for(fut, timeout=timeout_s)
        except TimeoutError as e:
            raise EmitWaitTimeout(
                f"emit_and_wait: emit() returned but no "
                f"{wait_for.__name__} matching {correlation_field}="
                f"{correlation!r} within {timeout_s}s"
            ) from e
        completed = True
        return result
    finally:
        if not completed and not emit_task.done():
            emit_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await emit_task
        _unregister(wait_for, correlation)
        _field_for.pop(key, None)
