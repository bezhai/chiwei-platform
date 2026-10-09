"""Interval clocks: one loop per clock that ticks every N seconds, plus a watchdog.

The plugin host runs the clocks its plugins register (``ctx.clock``, :mod:`app.host.host`)
through :class:`Clocks`.

**A tick is two steps.** ``tick(ts)`` is called inside the loop and returns the tick's work (an
awaitable). Anything ``tick`` itself raises, such as building a payload that is missing a field,
is a broken clock, not a bad tick: the loop records it, and the watchdog exits the process
(``os._exit(1)``) so PaaS restarts the pod. The work then runs on its own, fire-and-forget.

**Fire-and-forget.** The loop hands the work to a background task and goes straight to the next
tick, never waiting for it: a round that hangs or times out must not stop later ticks (world once
slept for good on coe because the loop awaited a round stuck on an LLM call). The work's errors
are logged and dropped; the next tick runs as usual. Work still running at :meth:`Clocks.stop`
is cancelled, so a hung round does not leak into the next process or test. Two ticks' work can
overlap; nodes that must not run twice at once guard themselves.

**Trace.** A tick has no inbound trace, so each one runs under
``trace_id = "interval:<seconds>s:<8 hex>"`` with no lane; otherwise the rounds it starts would
not be linked in Langfuse. ``seconds`` is a float (``interval:60.0s:…``).

**Cadence.** Fires are scheduled against a monotonic ``next_fire`` that advances by ``seconds``
whatever the work takes, so a slow tick does not skew the cadence.

**Lanes.** Time-based work does not run by default outside prod: test lanes may share prod's
data, and a second runner would cause real side effects there. The caller passes the decision
(:func:`app.runtime.lane_policy.time_sources_enabled_by_default`); skipped clocks are logged
with the escape hatch, ``DATAFLOW_ENABLE_TIME_SOURCES=1``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from app.runtime.lane_policy import current_deployment_lane
from app.runtime.propagation import Context, bind_context

logger = logging.getLogger(__name__)

# Called inside the clock loop; returns the tick's work. See the module docstring.
Tick = Callable[[datetime], Awaitable[None]]


@dataclass(frozen=True)
class Clock:
    """``name`` names the loop's task and the log lines; it is unique within one :class:`Clocks`."""

    name: str
    seconds: float
    tick: Tick


class Clocks:
    """The running clocks of one process, and the watchdog that exits it on a broken clock."""

    def __init__(self, owner: str) -> None:
        self._owner = owner
        self._loops: dict[str, asyncio.Task] = {}
        self._work: dict[str, set[asyncio.Task]] = {}
        self._error: BaseException | None = None
        self._broken: asyncio.Event | None = None
        self._watchdog: asyncio.Task | None = None

    async def start(self, clocks: Iterable[Clock], *, enabled: bool) -> None:
        """Start one loop per clock, or none when ``enabled`` is false, and the watchdog."""
        if self._watchdog is not None:
            raise RuntimeError("clocks already started; stop them first")
        clocks = list(clocks)
        loop = asyncio.get_running_loop()
        self._broken = asyncio.Event()
        if enabled:
            for clock in clocks:
                self._loops[clock.name] = loop.create_task(self._run(clock), name=clock.name)
        elif clocks:
            logger.warning(
                "runtime: app=%s skipped %d interval source(s) in lane=%s; "
                "set DATAFLOW_ENABLE_TIME_SOURCES=1 to run them intentionally",
                self._owner,
                len(clocks),
                current_deployment_lane() or "prod",
            )
        self._watchdog = loop.create_task(
            self._watch(), name=f"runtime-watchdog[{self._owner}]"
        )
        logger.info("runtime: app=%s started %d clock(s)", self._owner, len(self._loops))

    async def remove(self, name: str) -> None:
        """Stop one clock: its loop and its work still running. The others keep ticking."""
        loop_task = self._loops.pop(name, None)
        work = self._work.pop(name, set())
        await _cancel([loop_task, *work])

    async def stop(self) -> None:
        """Cancel every loop, the watchdog, and all work still running; wait for them to end."""
        tasks = [*self._loops.values(), self._watchdog]
        tasks += [t for work in self._work.values() for t in work]
        self._loops.clear()
        self._work.clear()
        self._watchdog = None
        self._broken = None
        await _cancel(tasks)

    async def _run(self, clock: Clock) -> None:
        loop = asyncio.get_running_loop()
        next_fire = loop.time() + clock.seconds
        try:
            while True:
                await asyncio.sleep(max(0.0, next_fire - loop.time()))
                ts = datetime.now(tz=UTC)
                next_fire += clock.seconds
                work = clock.tick(ts)
                trace_id = f"interval:{clock.seconds}s:{uuid.uuid4().hex[:8]}"
                self._spawn(clock.name, work, trace_id)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Classification: FATAL (contract §4.1, "payload build / clock setup"): the clock
            # itself is broken; the watchdog exits the process.
            self._record_error(clock.name, e)

    def _spawn(self, name: str, work: Awaitable[None], trace_id: str) -> None:
        async def run() -> None:
            try:
                async with bind_context(Context(trace_id=trace_id, lane=None)):
                    await work
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Classification: per-tick failure (contract §4.1): log, drop this tick's work,
                # keep ticking.
                logger.exception(
                    "runtime: clock %s tick raised %r; dropping this tick's work and continuing",
                    name,
                    exc,
                )

        task = asyncio.ensure_future(run())
        running = self._work.setdefault(name, set())
        running.add(task)

        def done(t: asyncio.Task) -> None:
            running.discard(t)
            # Cancelled before it started: close the work so it is not reported as a
            # coroutine that was never awaited.
            if inspect.iscoroutine(work) and inspect.getcoroutinestate(work) == "CORO_CREATED":
                work.close()

        task.add_done_callback(done)

    def _record_error(self, name: str, e: BaseException) -> None:
        """Keep the first fatal error (the one the watchdog reports) and wake the watchdog."""
        logger.exception("runtime: clock %s raised %r", name, e)
        if self._error is None:
            self._error = e
        if self._broken is not None:
            self._broken.set()

    async def _watch(self) -> None:
        """Exit the process once a clock broke. A normal stop cancels this before it fires."""
        assert self._broken is not None
        await self._broken.wait()
        if self._error is not None:
            logger.critical("runtime: clock fatal error %r, exiting process", self._error)
            os._exit(1)


async def _cancel(tasks: list[asyncio.Task | None]) -> None:
    present = [t for t in tasks if t is not None]
    for t in present:
        t.cancel()
    for t in present:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception as e:
            # Classification: HARMLESS teardown: one task's exit error must not stop the others
            # from being cleaned up.
            logger.warning("runtime: task %s exited with %r", t.get_name(), e)
