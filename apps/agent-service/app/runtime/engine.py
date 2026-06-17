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
from app.runtime.lane_policy import (
    current_deployment_lane,
    time_sources_enabled_by_default,
)
from app.runtime.migrator import plan_migration
from app.runtime.outbox_dispatcher import dispatcher_loop
from app.runtime.placement import DEFAULT_APP, known_apps, nodes_for_app
from app.runtime.source import SourceSpec
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
        time_sources_enabled: bool | None = None,
    ) -> None:
        self.app_name = app_name or os.getenv("APP_NAME") or DEFAULT_APP
        self._migrate_schema_on_run = migrate_schema_on_run
        self._time_sources_enabled = (
            time_sources_enabled_by_default()
            if time_sources_enabled is None
            else time_sources_enabled
        )
        self._source_tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None
        # First fatal error a source loop hit (so ``run()`` can re-raise
        # after cleanup). Any extra errors are logged but not saved —
        # reporting the first one is enough to fail the pod fast.
        self._source_error: BaseException | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._outbox_dispatcher_task: asyncio.Task | None = None
        # Fire-and-forget emit tasks spawned by cron / interval source loops.
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
        from app.runtime.outbox import RUNTIME_OUTBOX_DDL

        runtime_internal_stmts = (
            list(RUNTIME_INFLIGHT_DDL)
            + list(RUNTIME_DLQ_AUDIT_DDL)
            + list(RUNTIME_OUTBOX_DDL)
        )

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

    async def run(self) -> None:
        """Boot the runtime and block until cancelled."""
        if self._migrate_schema_on_run:
            await self.migrate_schema()
        # Register the runtime-internal delayed-trigger wire BEFORE
        # start_consumers (which calls compile_graph and freezes the
        # WIRING_REGISTRY snapshot). Skip silently when the configured
        # APP_NAME doesn't have a trigger route — emit_delayed durable
        # publishes will then fail-fast with a clear error rather than
        # crashing runtime startup.
        from app.infra.rabbitmq import KNOWN_APPS_FOR_DELAYED_TRIGGER
        from app.runtime.delayed_trigger import register_runtime_trigger_wire

        if self.app_name in KNOWN_APPS_FOR_DELAYED_TRIGGER:
            register_runtime_trigger_wire(self.app_name)
        else:
            logger.info(
                "runtime: app=%s has no delayed trigger route; "
                "emit_delayed(durability='durable') will be unavailable",
                self.app_name,
            )
        await start_consumers(app_name=self.app_name)
        # Outbox dispatcher needs DB. Tests opting out via
        # migrate_schema_on_run=False are signalling "no DB in this
        # process" and must also opt out of the dispatcher.
        if self._migrate_schema_on_run:
            self._outbox_dispatcher_task = asyncio.create_task(
                dispatcher_loop(), name="outbox_dispatcher"
            )
        await self.start_source_loops()
        try:
            assert self._stop_event is not None
            await self._stop_event.wait()
        finally:
            # Cancel the dispatcher before stopping consumers so any
            # in-flight emit() the dispatcher started can still complete
            # against a live wire/consumer. Same teardown order as
            # main.py lifespan.
            if self._outbox_dispatcher_task is not None:
                self._outbox_dispatcher_task.cancel()
                try:
                    await self._outbox_dispatcher_task
                except asyncio.CancelledError:
                    pass
            await self.stop_source_loops()
            await stop_consumers()

        if self._source_error is not None:
            raise self._source_error

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
            # Classification: FATAL contract violation. Surfaces to source-loop
            # outer try → _record_source_error → watchdog kill pod, matching the
            # "payload build / clock setup" fatal category in contract §4.1.
            raise RuntimeError(
                f"cron/interval source for {w.data_type.__name__} requires "
                f"a 'ts: str' field"
            ) from e

    def _record_source_error(self, name: str, e: BaseException) -> None:
        """Record the first fatal source-loop error and wake ``run()``.

        Subsequent errors are logged only — the first one is what we
        re-raise after cleanup.
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
        """Start cron / interval / mq source loops for nodes bound to this app.

        Also starts a watchdog task that monitors `_stop_event`. If a
        source loop hits a fatal error, watchdog calls ``os._exit(1)``
        so PaaS restarts the pod.

        Migrate / durable consumer / blocking 不在本方法范围 —— 调用方
        （main.py lifespan 或 Runtime.run()）自己负责。
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
                if src.kind == "cron":
                    if not self._time_sources_enabled:
                        skipped_time_sources += 1
                        continue
                    self._source_tasks.append(
                        loop.create_task(
                            self._source_loop_cron(w, src),
                            name=f"cron[{w.data_type.__name__}]",
                        )
                    )
                elif src.kind == "interval":
                    if not self._time_sources_enabled:
                        skipped_time_sources += 1
                        continue
                    self._source_tasks.append(
                        loop.create_task(
                            self._source_loop_interval(w, src),
                            name=f"interval[{w.data_type.__name__}]",
                        )
                    )
                elif src.kind == "mq":
                    self._source_tasks.append(
                        loop.create_task(
                            self._source_loop_mq(w, src),
                            name=f"mq[{w.data_type.__name__}]",
                        )
                    )

        if skipped_time_sources:
            lane = current_deployment_lane()
            logger.warning(
                "runtime: app=%s skipped %d cron/interval source(s) in lane=%s; "
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
        """Cancel + await every source task + watchdog (explicit cancel).

        Also drains the in-process scheduled task pool (Gap 9.2
        best_effort emit_delayed): pending best_effort tasks would
        otherwise leak into the next process instance.
        """
        from app.runtime.scheduled import cancel_all_scheduled

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
        cancel_all_scheduled()

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

    async def _source_loop_cron(self, w: WireSpec, src: SourceSpec) -> None:
        """Fire ``emit()`` for ``w`` each time the cron expression ticks.

        Uses ``croniter`` (5-field standard cron, 1-minute minimum) with
        the declared timezone (``src.params["tz"]``) so cron expressions
        are interpreted at the right wall clock.

        ``croniter.get_next`` is absolute-time based, so drift is
        naturally bounded to one tick. Fatal errors (bad payload shape,
        emit failure) surface via ``_source_error`` + ``_stop_event`` so
        ``run()`` can re-raise and the pod exits non-zero.

        Each tick auto-generates ``trace_id = f"cron:<expr>:<uuid8>"`` and
        binds it to the contextvar for the duration of ``emit()``. Without
        this, cron-triggered links broke trace continuity in Langfuse
        (cron source has no inbound trace_id). Lane is ``None`` (cron
        triggers don't carry a lane).
        """
        import uuid
        from zoneinfo import ZoneInfo

        from croniter import croniter

        from app.runtime.emit import emit
        from app.runtime.propagation import Context, bind_context

        expr = src.params["expr"]
        tz_name = src.params.get("tz", "UTC")
        zone = ZoneInfo(tz_name) if tz_name != "UTC" else UTC
        name = f"cron[{w.data_type.__name__}]"
        base = datetime.now(tz=zone)
        itr = croniter(expr, base)
        expr_slug = expr.replace(" ", "_")
        try:
            while True:
                next_ts = itr.get_next(datetime)
                delay = (next_ts - datetime.now(tz=zone)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                payload = self._build_payload(w, next_ts)
                trace_id = f"cron:{expr_slug}:{uuid.uuid4().hex[:8]}"
                # fire-and-forget（同 interval 心跳）：cron 也是时间推进循环，投出
                # 本 tick 即算下一个 cron 时刻、绝不同步等下游。一轮下游挂死 / 超
                # 时（如 day_review LLM 不返回）不能堵停后续 cron tick。emit 在后
                # 台 task 里跑、自带异常记录（见 _spawn_fire_and_forget_emit）。
                # 非 emit 路径（croniter 配置 / payload build / 时钟 setup）仍走外
                # 层 try → _record_source_error → watchdog kill pod。

                async def _emit_with_context(
                    payload=payload, trace_id=trace_id
                ) -> None:
                    async with bind_context(Context(trace_id=trace_id, lane=None)):
                        await emit(payload)

                self._spawn_fire_and_forget_emit(name, _emit_with_context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # 非 emit 路径 fatal: croniter 故障 / payload build (TypeError /
            # ValidationError → RuntimeError) / 时钟设置等启动期或 invariant 违反.
            self._record_source_error(name, e)
            return

    async def _source_loop_interval(self, w: WireSpec, src: SourceSpec) -> None:
        """Fire ``emit()`` for ``w`` every ``seconds`` seconds.

        Uses the event loop's monotonic clock to schedule fires against
        a rolling ``next_fire`` deadline. This prevents drift when
        ``emit()`` itself takes non-trivial time — otherwise
        ``asyncio.sleep(seconds)`` after a slow emit would permanently
        skew the cadence.

        Fatal errors surface via ``_source_error`` + ``_stop_event`` so
        ``run()`` re-raises and the pod exits non-zero.

        Each tick auto-generates ``trace_id = f"interval:<seconds>s:<uuid8>"``
        bound for the duration of ``emit()``. See ``_source_loop_cron`` for
        rationale (Gap 11 — Langfuse trace continuity).
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

    async def _source_loop_mq(self, w: WireSpec, src: SourceSpec) -> None:
        """Consume JSON frames from a RabbitMQ queue into ``w``'s target @node.

        Contract (see T1.4.5 in the Phase 0+1 plan):

        * the target is a single @node whose signature is
          ``(req: XxxData) -> ...``; ``compile_graph`` already enforces
          this shape, so we can safely take the first (only) entry of
          ``inputs_of(target)`` as the decode class;
        * the queue is ``lane_queue(src.params["queue"], current_lane())``
          so lane isolation + TTL fallback behave the same as every
          other MQ consumer in the process;
        * declare is idempotent (``durable=True, auto_delete=False,
          passive=False``) — if channel-server or ``declare_topology``
          already created the queue, re-declare is a no-op;
        * ``prefetch_count=10`` moves the old ``semaphore(10)`` back-
          pressure to the broker; handlers can stay single-task;
        * per-message context: mirror ``durable.py::_build_handler`` —
          read ``trace_id`` / ``lane`` headers with defensive coercion,
          set both contextvars for the duration of the node call, reset
          in ``finally``;
        * ack semantics: ``message.process(requeue=False)``. Decode
          failures (``JSONDecodeError`` / ``ValidationError``) are
          logged + continue (message gets acked on context exit — poison
          frames don't loop). Business exceptions from the node bubble
          out of ``process`` so aio-pika nacks and the DLX catches them;
        * shutdown: the outer ``asyncio.Task`` is cancelled by
          ``run()``'s ``finally``. ``CancelledError`` unwinds both the
          ``async for`` and the ``queue.iterator()`` context cleanly;
          no extra consumer-tag tracking needed.
        """
        import json as _json
        import uuid as _uuid

        from pydantic import ValidationError as _ValidationError

        from app.infra.rabbitmq import current_lane, lane_queue, mq
        from app.runtime.node import inputs_of
        from app.runtime.propagation import Context, bind_context, extract_context

        if len(w.consumers) != 1:
            # compile_graph should have caught this; guard anyway so a
            # skipped-validation path can't silently drop frames.
            raise RuntimeError(
                f"MQSource for {w.data_type.__name__}: expected 1 consumer, "
                f"got {len(w.consumers)}"
            )
        (target,) = w.consumers
        ins = inputs_of(target)
        if len(ins) != 1:
            raise RuntimeError(
                f"MQSource target {target.__name__} must take exactly 1 "
                f"Data arg; got signature {ins}"
            )
        param_name, req_cls = next(iter(ins.items()))

        queue_base = src.params["queue"]
        name = f"mq[{w.data_type.__name__}/{queue_base}]"

        await mq.connect()
        # ``mq.connect`` already sets a channel-level prefetch_count=10,
        # but we open a *fresh* channel here so consumer back-pressure is
        # isolated from the shared publisher channel (otherwise slow MQ
        # handlers would starve unrelated publishers on the same pod).
        assert mq._connection is not None  # type: ignore[attr-defined]
        channel = await mq._connection.channel()  # type: ignore[attr-defined]
        await channel.set_qos(prefetch_count=10)

        actual_queue = lane_queue(queue_base, current_lane())

        # Lazy lane-queue declare: declare_topology runs at fixture/boot
        # time using the lane that was current then. If LANE was set
        # later (test code, late lifespan hook), the lane queue may not
        # exist yet — passive get_queue would NOT_FOUND crash. Look up
        # the route in ALL_ROUTES and ask mq to ensure the lane queue
        # is declared with the route's exact args (idempotent). For
        # queues outside ALL_ROUTES (legacy adoption tests), skip and
        # fall through to passive get_queue as before.
        from app.infra.rabbitmq import ALL_ROUTES

        for r in ALL_ROUTES:
            if r.queue == queue_base:
                lane = current_lane()
                if lane:
                    await mq._ensure_lane_queue(r, lane)  # type: ignore[attr-defined]
                break

        try:
            # Passive fetch: the queue is owned by whoever publishes to
            # it (channel-server for ``chat_request``, ``declare_topology``
            # for the static ALL_ROUTES set). Trying to re-declare with our
            # own args would clash on DLX / TTL settings — see
            # ``_build_queue_args`` in ``app/infra/rabbitmq.py``. Mirror
            # ``mq.consume``'s ``get_queue`` for exactly that reason.
            queue = await channel.get_queue(actual_queue)
        except Exception as e:
            # Classification: FATAL infra (contract §4.1 row "Source loop infra
            # 故障"). Queue missing at consume time is a deployment-level bug
            # (topology / publisher ordering). Fail fast so PaaS restarts
            # us and the operator sees the real cause.
            self._record_source_error(name, e)
            try:
                await channel.close()
            except Exception:  # pragma: no cover
                # HARMLESS cleanup: best-effort close on the error path;
                # the connection is already in a bad state, swallow either way.
                pass
            return

        logger.info(
            "runtime: mq source started queue=%s target=%s",
            actual_queue,
            target.__name__,
        )

        try:
            async with queue.iterator() as qit:
                async for incoming in qit:
                    ctx = extract_context(incoming.headers)
                    # Gap 11 mq-source fallback: external producers
                    # (e.g. channel-server publishing CHAT_REQUEST) may not
                    # inject trace_id into the message header. Without a
                    # fallback the entire downstream chain runs with
                    # trace_id=None — runtime_inflight rows, langfuse
                    # spans, and durable retries all lose continuity.
                    # Mirror cron / interval source's auto-generation
                    # (Task 3) at this external boundary.
                    if ctx.trace_id is None:
                        ctx = Context(
                            trace_id=f"mq:{queue_base}:{_uuid.uuid4().hex[:8]}",
                            lane=ctx.lane,
                        )
                    try:
                        # ``process(requeue=False)`` context-manager:
                        # clean exit -> ack; raised exception -> nack
                        # without requeue -> aio-pika dead-letters via
                        # DLX. We let the raise escape *process* but
                        # catch it right after so one bad @node call
                        # never terminates the outer loop (equivalent to
                        # ``@mq_error_handler`` in legacy workers).
                        logger.info(
                            "mq source %s: received frame (%d bytes)",
                            actual_queue,
                            len(incoming.body),
                        )
                        async with incoming.process(requeue=False):
                            async with bind_context(ctx):
                                try:
                                    body = _json.loads(incoming.body.decode())
                                    # MQSource adapts external producers whose
                                    # payloads may carry fields meant for other
                                    # consumers (e.g. channel-server adds 'lane' to
                                    # {"message_id"} — we read lane from
                                    # headers). Data's extra='forbid' is a
                                    # deliberate policy on our internal
                                    # contracts; filter to the Data class's
                                    # declared fields before handing off so the
                                    # strict policy stays, and external slack is
                                    # absorbed here.
                                    body = {
                                        k: v
                                        for k, v in body.items()
                                        if k in req_cls.model_fields
                                    }
                                    req = req_cls(**body)
                                except (
                                    _json.JSONDecodeError,
                                    UnicodeDecodeError,
                                    _ValidationError,
                                    TypeError,
                                ) as e:
                                    # Bad frame: log + dead-letter. We log
                                    # warning here so the body preview lands
                                    # in the standard log stream (the DLQ
                                    # itself only carries the raw body), then
                                    # raise so ``process(requeue=False)``
                                    # nacks the message and the broker routes
                                    # it to the DLX. requeue=False rules out
                                    # any poison loop; the outer ``except
                                    # Exception`` below catches the re-raise
                                    # so the source loop keeps draining.
                                    logger.warning(
                                        "mq source %s decode failed: %s body=%r",
                                        actual_queue,
                                        e,
                                        incoming.body[:200],
                                    )
                                    raise
                                await target(**{param_name: req})
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # Classification: PER-MESSAGE routed (contract §4.1
                        # rows "MQ Source 单条消息 decode 失败" + "MQ Source
                        # 内 @node target 抛 Exception"). Both surface through
                        # ``process(requeue=False)`` -> DLX. The loop must
                        # stay alive to drain subsequent messages, so we log
                        # and move on instead of tripping _record_source_error
                        # (which would kill the pod for a single bad message).
                        logger.exception(
                            "mq source %s: target %s message DLX'd (%r); continuing",
                            actual_queue,
                            target.__name__,
                            e,
                        )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Classification: FATAL infra (contract §4.1 "Source loop infra
            # 故障"). Only channel/connection death + consume-iterator setup
            # failure reach here — those *are* fatal and should restart the pod.
            self._record_source_error(name, e)
            return
        finally:
            # ``queue.iterator()`` teardown already cancels the implicit
            # consumer tag; closing the channel releases the underlying
            # RabbitMQ resources so we don't leak channels across
            # Runtime.stop/start cycles in tests.
            try:
                await channel.close()
            except Exception:  # pragma: no cover — best-effort
                # HARMLESS cleanup: channel may already be closed by a prior
                # error path; swallow rather than mask the original exception.
                pass
