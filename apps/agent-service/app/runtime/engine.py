"""Runtime: orchestrates startup for one deployment (one app).

A process boots ``Runtime(app_name=...).run()`` (default ``app_name``
falls back to ``placement.DEFAULT_APP``). Responsibilities:

  1. **Migrate schema** — introspect ``information_schema`` and apply
     the additive DDL plan for every registered ``Data`` class.
  2. **Start durable consumers** — filtered to the wires whose consumers
     are bound to this app (see ``start_consumers(app_name)``).
  3. **Start source loops** — one background task per ``cron`` /
     ``interval`` source attached to a wire whose consumers belong here.
  4. **Keep running** — block until cancelled; on cancel, stop the
     background tasks and the durable consumers.

What Runtime does *not* do: wiring imports. The caller (Phase 1's
``app/workers/runtime_entry.py``) is responsible for importing the
modules that register ``@node`` / ``wire()`` / ``bind()`` before calling
``run()``. Runtime only sees whatever was already registered.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from pydantic import ValidationError

from app.runtime.data import DATA_REGISTRY
from app.runtime.durable import start_consumers, stop_consumers
from app.runtime.graph import compile_graph
from app.runtime.migrator import plan_migration
from app.runtime.placement import DEFAULT_APP, nodes_for_app
from app.runtime.wire import WireSpec

logger = logging.getLogger(__name__)


class Runtime:
    """Single-process entrypoint for a dataflow deployment.

    ``app_name`` determines which subset of the wired graph this process
    serves. Resolution order:

      1. explicit ``app_name=`` kwarg,
      2. ``APP_NAME`` environment variable,
      3. ``placement.DEFAULT_APP`` ("agent-service").

    ``migrate_schema_on_run`` defaults to ``True``; set ``False`` for
    tests that already control the schema (e.g. via the ``test_db``
    fixture with a pre-migrated table) or that don't need DB at all.
    """

    def __init__(
        self,
        app_name: str | None = None,
        *,
        migrate_schema_on_run: bool = True,
    ) -> None:
        self.app_name = app_name or os.getenv("APP_NAME") or DEFAULT_APP
        self._migrate_schema_on_run = migrate_schema_on_run
        self._source_tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None

    async def migrate_schema(self) -> None:
        """Read live schema from PostgreSQL, diff against ``DATA_REGISTRY``,
        apply additive DDL.

        Runs outside transactions (per-statement auto-commit via the
        SQLAlchemy session's implicit transaction) so partial progress
        survives if a later statement trips a MigrationError. Breaking
        changes are refused by ``plan_migration`` itself — we never see
        a destructive statement here.
        """
        from sqlalchemy import text

        from app.data.session import get_session

        # Read live schema: information_schema.columns gives us
        # {table: {column: pg_type}} for the public schema.
        existing: dict[str, dict[str, str]] = {}
        async with get_session() as s:
            result = await s.execute(
                text(
                    "SELECT table_name, column_name, data_type "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public'"
                )
            )
            for table_name, column_name, data_type in result.all():
                existing.setdefault(table_name, {})[column_name] = data_type

        plan = plan_migration(list(DATA_REGISTRY), existing)

        if not plan.stmts:
            logger.info("runtime: schema migration plan is empty, nothing to do")
            return

        # ``plan_migration`` only emits parameterless DDL today
        # (CREATE TABLE / ALTER TABLE ADD COLUMN / CREATE INDEX), so we
        # can simply text()-execute each statement. If the migrator ever
        # starts emitting parameterised statements, this loop needs to
        # re-map the positional ``Stmt.params`` into a named bind dict.
        async with get_session() as s:
            for stmt in plan.stmts:
                if stmt.params:
                    raise RuntimeError(
                        "Runtime.migrate_schema does not support parameterised "
                        "DDL statements yet; got: "
                        f"sql={stmt.sql!r} params={stmt.params!r}"
                    )
                await s.execute(text(stmt.sql))
        logger.info(
            "runtime: applied %d schema migration statement(s)",
            len(plan.stmts),
        )

    async def run(self) -> None:
        """Boot the runtime and block until cancelled.

        On ``asyncio.CancelledError`` (or any exception from a source
        loop surfaced via ``gather``), stop sources + durable consumers
        cleanly before re-raising.
        """
        if self._migrate_schema_on_run:
            await self.migrate_schema()

        await start_consumers(app_name=self.app_name)

        graph = compile_graph()
        allowed_nodes = nodes_for_app(self.app_name)
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()

        try:
            for w in graph.wires:
                # Only run source loops for wires whose consumers all belong
                # to this app — otherwise we'd fire nodes that aren't running
                # here (or double-fire if another app also runs the source).
                if not w.consumers:
                    continue
                if not all(c in allowed_nodes for c in w.consumers):
                    continue
                for src in w.sources:
                    if src.kind == "cron":
                        self._source_tasks.append(
                            loop.create_task(
                                self._source_loop_cron(w, src.params["expr"]),
                                name=f"cron[{w.data_type.__name__}]",
                            )
                        )
                    elif src.kind == "interval":
                        self._source_tasks.append(
                            loop.create_task(
                                self._source_loop_interval(w, src.params["seconds"]),
                                name=f"interval[{w.data_type.__name__}]",
                            )
                        )
                    # Other source kinds (http / mq / feishu_webhook / manual)
                    # are wired elsewhere (FastAPI routes, legacy bridges):
                    # Runtime only owns the time-triggered ones.

            logger.info(
                "runtime: app=%s started (%d source task(s))",
                self.app_name,
                len(self._source_tasks),
            )

            # Block forever unless cancelled externally.
            await self._stop_event.wait()
        finally:
            for t in self._source_tasks:
                t.cancel()
            # Let each task observe its cancellation exactly once.
            for t in self._source_tasks:
                try:
                    await t
                except (asyncio.CancelledError, Exception) as e:
                    if not isinstance(e, asyncio.CancelledError):
                        logger.warning(
                            "runtime: source task %s exited with %r",
                            t.get_name(),
                            e,
                        )
            self._source_tasks.clear()
            await stop_consumers()

    # ------------------------------------------------------------------
    # source loops
    # ------------------------------------------------------------------

    def _build_payload(self, w: WireSpec, ts: datetime):
        """Construct ``w.data_type(ts=<iso>)`` for time-triggered sources.

        By convention cron/interval sources emit a single-field Data
        carrying the tick timestamp. If the data type doesn't accept a
        ``ts: str`` kwarg, raise loudly rather than silently dropping
        ticks.
        """
        try:
            return w.data_type(ts=ts.isoformat())
        except (TypeError, ValidationError) as e:
            raise RuntimeError(
                f"cron/interval source for {w.data_type.__name__} requires "
                f"a 'ts: str' field"
            ) from e

    async def _source_loop_cron(self, w: WireSpec, expr: str) -> None:
        """Fire emit() for ``w`` each time the cron expression ticks.

        Uses ``croniter`` (5-field standard cron, 1-minute minimum).
        """
        from croniter import croniter

        from app.runtime.emit import emit

        base = datetime.now(tz=UTC)
        itr = croniter(expr, base)
        while True:
            next_ts = itr.get_next(datetime)
            delay = (next_ts - datetime.now(tz=UTC)).total_seconds()
            if delay > 0:
                await asyncio.sleep(delay)
            payload = self._build_payload(w, next_ts)
            try:
                await emit(payload)
            except Exception as e:
                logger.exception(
                    "runtime: cron emit for %s raised %r",
                    w.data_type.__name__,
                    e,
                )

    async def _source_loop_interval(self, w: WireSpec, seconds: float) -> None:
        """Fire emit() for ``w`` every ``seconds`` seconds.

        Uses a simple ``asyncio.sleep(seconds)`` cadence — drift is
        acceptable for sub-minute periodic sources (they're advisory
        ticks, not billing events).
        """
        from app.runtime.emit import emit

        while True:
            await asyncio.sleep(seconds)
            payload = self._build_payload(w, datetime.now(tz=UTC))
            try:
                await emit(payload)
            except Exception as e:
                logger.exception(
                    "runtime: interval emit for %s raised %r",
                    w.data_type.__name__,
                    e,
                )
