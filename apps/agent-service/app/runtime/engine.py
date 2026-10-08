"""Runtime: schema migration and the interval sources of one deployment (one app).

``app.main``'s lifespan builds ``Runtime(app_name=...)`` and drives it:

  1. **Migrate schema** — :func:`app.runtime.migrator.migrate_schema`.
  2. **Start source loops** — one clock per ``interval`` source attached to a wire whose
     consumers belong here; each tick builds the wire's Data and emits it. The clocks themselves
     (fire-and-forget ticks, trace ids, lane gating, the watchdog) are
     :class:`app.runtime.clock.Clocks`, which the plugin host runs too;
     ``stop_source_loops`` stops them.

What Runtime does *not* do: wiring imports, durable consumers,
messaging. The lifespan imports the app's wiring (through
``app.runtime.bootstrap.prepare_for_run``) and starts the consumers
itself; Runtime only sees whatever was already registered.
"""

from __future__ import annotations

import os
from datetime import datetime

from pydantic import ValidationError

from app.runtime.clock import Clock, Clocks, Tick
from app.runtime.graph import compile_graph
from app.runtime.lane_policy import time_sources_enabled_by_default
from app.runtime.migrator import migrate_schema
from app.runtime.placement import DEFAULT_APP, known_apps, nodes_for_app
from app.runtime.wire import WireSpec


class Runtime:
    """Schema migration and source loops for one dataflow deployment.

    ``app_name`` determines which subset of the wired graph this process
    serves. Resolution order:

      1. explicit ``app_name=`` kwarg,
      2. ``APP_NAME`` environment variable,
      3. ``placement.DEFAULT_APP`` ("agent-service").
    """

    def __init__(
        self,
        app_name: str | None = None,
        *,
        time_sources_enabled: bool | None = None,
    ) -> None:
        self.app_name = app_name or os.getenv("APP_NAME") or DEFAULT_APP
        self._time_sources_enabled = (
            time_sources_enabled_by_default()
            if time_sources_enabled is None
            else time_sources_enabled
        )
        self._clocks: Clocks | None = None

    async def migrate_schema(self) -> None:
        """See :func:`app.runtime.migrator.migrate_schema`."""
        await migrate_schema()

    def _build_payload(self, w: WireSpec, ts: datetime):
        """Construct ``w.data_type(ts=<iso>)`` for time-triggered sources.

        By convention interval sources emit a single-field Data
        carrying the tick timestamp. If the data type doesn't accept a
        ``ts: str`` kwarg, raise loudly rather than silently dropping
        ticks.
        """
        try:
            return w.data_type(ts=ts.isoformat())
        except (TypeError, ValidationError) as e:
            # Classification: FATAL contract violation. Raised inside the clock loop (see
            # _ticker), so it reaches the watchdog and kills the pod, matching the
            # "payload build / clock setup" fatal category in contract §4.1.
            raise RuntimeError(
                f"interval source for {w.data_type.__name__} requires "
                f"a 'ts: str' field"
            ) from e

    def _ticker(self, w: WireSpec) -> Tick:
        """The clock's tick for ``w``: build the payload now, in the loop; emit it as the work."""

        def tick(ts: datetime):
            from app.runtime.emit import emit

            return emit(self._build_payload(w, ts))

        return tick

    async def start_source_loops(self) -> None:
        """Start the interval sources of the nodes bound to this app, and the watchdog.

        Migrate / durable consumer 不在本方法范围 —— 调用方（main.py
        lifespan）自己负责。
        """
        if self._clocks is not None:
            raise RuntimeError(
                "start_source_loops already called; call stop_source_loops() first"
            )

        valid = known_apps()
        if self.app_name not in valid:
            raise RuntimeError(
                f"start_source_loops for app={self.app_name!r}: "
                f"no @node bound there (known: {sorted(valid)})"
            )

        graph = compile_graph()
        allowed_nodes = nodes_for_app(self.app_name)
        clocks = [
            Clock(
                name=f"interval[{w.data_type.__name__}]",
                seconds=src.params["seconds"],
                tick=self._ticker(w),
            )
            for w in graph.wires
            if w.consumers and all(c in allowed_nodes for c in w.consumers)
            for src in w.sources
            if src.kind == "interval"
        ]
        self._clocks = Clocks(self.app_name)
        await self._clocks.start(clocks, enabled=self._time_sources_enabled)

    async def stop_source_loops(self) -> None:
        """Stop every clock, its ticks still running, and the watchdog."""
        if self._clocks is None:
            return
        clocks, self._clocks = self._clocks, None
        await clocks.stop()
