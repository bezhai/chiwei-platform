"""Runtime: schema migration and the source loops of one deployment (one app).

``app.main``'s lifespan builds ``Runtime(app_name=...)`` and drives it:

  1. **Migrate schema** — introspect ``information_schema`` and apply
     the additive DDL plan for every registered ``Data`` class.
  2. **Start source loops** — one background task per ``interval``
     source attached to a wire whose consumers belong here, plus a
     watchdog that exits the process on a fatal loop error;
     ``stop_source_loops`` cancels them.

What Runtime does *not* do: wiring imports, durable consumers,
messaging. The lifespan imports the app's wiring (through
``app.runtime.bootstrap.prepare_for_run``) and starts the consumers
itself; Runtime only sees whatever was already registered.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime

from pydantic import ValidationError

from app.runtime.data import DATA_REGISTRY
from app.runtime.graph import compile_graph
from app.runtime.lane_policy import (
    current_deployment_lane,
    time_sources_enabled_by_default,
)
from app.runtime.migrator import plan_migration
from app.runtime.placement import DEFAULT_APP, known_apps, nodes_for_app
from app.runtime.source import SourceSpec
from app.runtime.wire import WireSpec

logger = logging.getLogger(__name__)


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
        self._source_tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None
        # First fatal error a source loop hit (the watchdog exits the
        # process on it). Any extra errors are logged but not saved —
        # reporting the first one is enough to fail the pod fast.
        self._source_error: BaseException | None = None
        self._watchdog_task: asyncio.Task | None = None
        # Fire-and-forget emit tasks spawned by the interval source loops.
        # The time-advancing loop投出心跳即进下一拍、绝不同步等下游——下游一轮挂
        # 死不能堵停后续心跳（world 永睡的真机机制）。每条 emit 跑在独立后台
        # task 里，自带 try-except 记录异常（不静默吞、不留 "Task exception was
        # never retrieved"），并被 stop_source_loops 取消、防止跨 Runtime 重启泄漏。
        self._fire_and_forget_emits: set[asyncio.Task] = set()

    async def migrate_schema(self) -> None:
        """Read live schema from PostgreSQL, diff against ``DATA_REGISTRY``,
        apply additive DDL.

        The entire migration plan is applied atomically inside a single
        transaction (the ``get_session()`` context manager commits on
        clean exit, rolls back on exception). If any statement in the
        plan raises, the whole migration rolls back — the DB stays in
        its pre-migration state and the process must be retried.

        ``plan_migration`` already refuses destructive statements, so we
        only ever issue additive, ordered DDL here — atomic apply is the
        safe choice for that shape.

        Known limitation: this targets the ``public`` PostgreSQL schema
        only. Services that share a database but want isolated schemas
        would need a separate migration entrypoint.
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

        # Always apply runtime-internal DDL (idempotent IF NOT EXISTS),
        # regardless of whether the Data plan is empty — these tables are
        # framework state, not Data, and aren't tracked by plan_migration.
        from app.runtime.dlq_audit import RUNTIME_DLQ_AUDIT_DDL
        from app.runtime.inflight import RUNTIME_INFLIGHT_DDL

        runtime_internal_stmts = list(RUNTIME_INFLIGHT_DDL) + list(RUNTIME_DLQ_AUDIT_DDL)

        if not plan.stmts and not runtime_internal_stmts:
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
            for sql in runtime_internal_stmts:
                await s.execute(text(sql))
        logger.info(
            "runtime: applied %d Data + %d runtime-internal migration statement(s)",
            len(plan.stmts),
            len(runtime_internal_stmts),
        )

    # ------------------------------------------------------------------
    # source loops
    # ------------------------------------------------------------------

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
            # Classification: FATAL contract violation. Surfaces to source-loop
            # outer try → _record_source_error → watchdog kill pod, matching the
            # "payload build / clock setup" fatal category in contract §4.1.
            raise RuntimeError(
                f"interval source for {w.data_type.__name__} requires "
                f"a 'ts: str' field"
            ) from e

    def _record_source_error(self, name: str, e: BaseException) -> None:
        """Record the first fatal source-loop error and wake the watchdog.

        Subsequent errors are logged only — the first one is what the
        watchdog reports before it exits the process.
        """
        logger.exception("runtime: source loop %s raised %r", name, e)
        if self._source_error is None:
            self._source_error = e
        if self._stop_event is not None:
            self._stop_event.set()

    def _spawn_fire_and_forget_emit(self, name: str, make_coro) -> None:
        """Run one tick's ``emit()`` as a tracked background task.

        ``make_coro`` is a zero-arg factory returning the ``emit`` coroutine
        (not the coroutine itself). The coroutine is only instantiated inside
        ``_runner`` right before it's awaited — so a tick that races a
        cancellation never leaves a created-but-unawaited coroutine ("coroutine
        was never awaited" warning).

        This is the存活地基: the time-advancing source loop must投出心跳即返回、
        绝不同步 ``await`` 下游。一轮下游（world 推演）挂死 / 超时不能堵停后续
        心跳——这正是真机 coe 实测世界睡死的机制（同步 ``await emit`` 被一轮卡
        死的 LLM 永久堵住）。所以每拍把 emit 甩进独立后台 task 立刻进下一拍。

        正确性两点（不是裸 ``create_task(emit(...))``）：
          1. **异常不丢**：runner 包 try-except，下游抛异常时 ``log.exception``
             记录而非静默丢失（裸 task 会触发 "Task exception was never
             retrieved" 警告且异常被 GC 吞）。下游 emit 异常不是 fatal——单拍失
             败下一拍照常（对齐旧同步路径 §4.1 "emit 抛 Exception = log + 继续
             下一 tick"）。
          2. **不泄漏**：task 登记进 ``_fire_and_forget_emits``，``stop_source_
             loops`` 取消所有未完成的，避免挂死的 emit 跨 Runtime 重启 / 测试
             泄漏成 "Task was destroyed but it is pending"。
        防重入（同 key 下游并发叠加烧钱）由下游节点自身的 single-flight 锁挡
        （world_tick 的 ``single_flight(f"world:{lane}")``）——fire-and-forget 不
        改这层：第二拍的 emit 仍流经下游、仍撞锁、仍丢弃，不会真的并发推演。
        """

        async def _runner() -> None:
            try:
                await make_coro()
            except asyncio.CancelledError:
                raise
            except Exception as emit_exc:
                logger.exception(
                    "runtime: source %s fire-and-forget emit() raised %r; "
                    "dropping this tick's downstream and continuing",
                    name,
                    emit_exc,
                )

        task = asyncio.ensure_future(_runner())
        self._fire_and_forget_emits.add(task)
        task.add_done_callback(self._fire_and_forget_emits.discard)

    async def start_source_loops(self) -> None:
        """Start the interval source loops for nodes bound to this app.

        Also starts a watchdog task that monitors `_stop_event`. If a
        source loop hits a fatal error, watchdog calls ``os._exit(1)``
        so PaaS restarts the pod.

        Migrate / durable consumer 不在本方法范围 —— 调用方（main.py
        lifespan）自己负责。
        """
        if self._source_tasks or self._watchdog_task is not None:
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
        loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        skipped_time_sources = 0

        for w in graph.wires:
            if not w.consumers:
                continue
            if not all(c in allowed_nodes for c in w.consumers):
                continue
            for src in w.sources:
                if src.kind == "interval":
                    if not self._time_sources_enabled:
                        skipped_time_sources += 1
                        continue
                    self._source_tasks.append(
                        loop.create_task(
                            self._source_loop_interval(w, src),
                            name=f"interval[{w.data_type.__name__}]",
                        )
                    )

        if skipped_time_sources:
            lane = current_deployment_lane()
            logger.warning(
                "runtime: app=%s skipped %d interval source(s) in lane=%s; "
                "set DATAFLOW_ENABLE_TIME_SOURCES=1 to run them intentionally",
                self.app_name,
                skipped_time_sources,
                lane or "prod",
            )

        self._watchdog_task = loop.create_task(
            self._watch_source_error(),
            name=f"runtime-watchdog[{self.app_name}]",
        )

        logger.info(
            "runtime: app=%s start_source_loops (%d source task(s))",
            self.app_name,
            len(self._source_tasks),
        )

    async def stop_source_loops(self) -> None:
        """Cancel + await every source task + watchdog (explicit cancel)."""
        for t in self._source_tasks:
            t.cancel()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
        # Cancel any in-flight fire-and-forget emit tasks too. A downstream
        # round that's still hung (the whole reason we don't await it) would
        # otherwise leak into the next process instance / test as a "Task was
        # destroyed but it is pending" warning. Snapshot first because each
        # task's done-callback mutates the set.
        fire_and_forget = list(self._fire_and_forget_emits)
        for t in fire_and_forget:
            t.cancel()
        for t in [*self._source_tasks, self._watchdog_task, *fire_and_forget]:
            if t is None:
                continue
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception as e:
                # Classification: HARMLESS teardown swallow (contract §4 not
                # applicable—不是消息处理路径，是 stop 阶段 await 已取消任务).
                # 单个任务退出态报错不影响后续任务清理，记一条 warning 即可.
                logger.warning("runtime: task %s exited with %r", t.get_name(), e)
        self._source_tasks.clear()
        self._fire_and_forget_emits.clear()
        self._watchdog_task = None
        self._stop_event = None

    async def _watch_source_error(self) -> None:
        """Wait for `_stop_event`; on fire (with `_source_error` set),
        log fatal + ``os._exit(1)``.

        Normal shutdown 不走这条 —— stop_source_loops cancels this task
        directly so it never reads `_source_error`.
        """
        assert self._stop_event is not None
        await self._stop_event.wait()
        if self._source_error is not None:
            logger.critical(
                "runtime: source loop fatal error %r, exiting process",
                self._source_error,
            )
            os._exit(1)

    async def _source_loop_interval(self, w: WireSpec, src: SourceSpec) -> None:
        """Fire ``emit()`` for ``w`` every ``seconds`` seconds.

        Uses the event loop's monotonic clock to schedule fires against
        a rolling ``next_fire`` deadline. This prevents drift when
        ``emit()`` itself takes non-trivial time — otherwise
        ``asyncio.sleep(seconds)`` after a slow emit would permanently
        skew the cadence.

        Fatal errors surface via ``_source_error`` + ``_stop_event`` so
        the watchdog exits the pod non-zero.

        Each tick auto-generates ``trace_id = f"interval:<seconds>s:<uuid8>"``
        and binds it for the duration of ``emit()``: an interval source has
        no inbound trace_id, and without one the triggered links break
        trace continuity in Langfuse (Gap 11). Lane is ``None`` (a tick
        doesn't carry a lane).
        """
        import uuid

        from app.runtime.emit import emit
        from app.runtime.propagation import Context, bind_context

        seconds = src.params["seconds"]
        name = f"interval[{w.data_type.__name__}]"
        loop = asyncio.get_event_loop()
        next_fire = loop.time() + seconds
        try:
            while True:
                sleep_for = max(0.0, next_fire - loop.time())
                await asyncio.sleep(sleep_for)
                ts = datetime.now(tz=UTC)
                next_fire += seconds
                payload = self._build_payload(w, ts)
                trace_id = f"interval:{seconds}s:{uuid.uuid4().hex[:8]}"
                # 保底心跳 = fire-and-forget：投出本拍即进下一拍循环、绝不同步
                # 等下游。一轮下游（world 推演）挂死 / 超时都不堵停后续心跳
                # （world 永睡的真机机制）。next_fire 已在 emit 前 += seconds，
                # 下一拍调度时刻不受本拍下游耗时影响。emit 在后台 task 里跑、
                # 自带异常记录（见 _spawn_fire_and_forget_emit）。

                async def _emit_with_context(
                    payload=payload, trace_id=trace_id
                ) -> None:
                    async with bind_context(Context(trace_id=trace_id, lane=None)):
                        await emit(payload)

                self._spawn_fire_and_forget_emit(name, _emit_with_context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 非 emit 路径 fatal: 时钟 setup / payload build / 时钟相关故障.
            self._record_source_error(name, e)
            return
