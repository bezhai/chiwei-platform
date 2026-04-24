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
    ) -> None:
        self.app_name = app_name or os.getenv("APP_NAME") or DEFAULT_APP
        self._migrate_schema_on_run = migrate_schema_on_run
        self._source_tasks: list[asyncio.Task] = []
        self._stop_event: asyncio.Event | None = None
        # First fatal error a source loop hit (so ``run()`` can re-raise
        # after cleanup). Any extra errors are logged but not saved —
        # reporting the first one is enough to fail the pod fast.
        self._source_error: BaseException | None = None

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

        On ``asyncio.CancelledError`` (or any fatal exception surfaced
        by a source loop via ``self._source_error``), stop sources +
        durable consumers cleanly before re-raising so the pod exits
        non-zero and PaaS restarts it. A silent ``run()`` return on a
        dead source is not acceptable — PaaS would see the pod healthy
        while the source does nothing.
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
                                self._source_loop_cron(w, src),
                                name=f"cron[{w.data_type.__name__}]",
                            )
                        )
                    elif src.kind == "interval":
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
                    # Other source kinds (http / feishu_webhook / manual)
                    # are wired elsewhere (FastAPI routes, legacy bridges):
                    # Runtime only owns the time-triggered + MQ-triggered ones.

            logger.info(
                "runtime: app=%s started (%d source task(s))",
                self.app_name,
                len(self._source_tasks),
            )

            # Block forever unless cancelled externally, or until a
            # source loop sets ``_source_error`` and wakes us up.
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

        # After cleanup: if a source loop surfaced a fatal error, raise
        # it so the worker process exits non-zero. This runs *after* the
        # finally block by design — we want consumers stopped first.
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

    async def _source_loop_cron(self, w: WireSpec, src: SourceSpec) -> None:
        """Fire ``emit()`` for ``w`` each time the cron expression ticks.

        Uses ``croniter`` (5-field standard cron, 1-minute minimum).
        ``croniter.get_next`` is absolute-time based, so drift is
        naturally bounded to one tick. Fatal errors (bad payload shape,
        emit failure) surface via ``_source_error`` + ``_stop_event`` so
        ``run()`` can re-raise and the pod exits non-zero.
        """
        from croniter import croniter

        from app.runtime.emit import emit

        expr = src.params["expr"]
        name = f"cron[{w.data_type.__name__}]"
        base = datetime.now(tz=UTC)
        itr = croniter(expr, base)
        try:
            while True:
                next_ts = itr.get_next(datetime)
                delay = (next_ts - datetime.now(tz=UTC)).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                payload = self._build_payload(w, next_ts)
                await emit(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
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
        """
        from app.runtime.emit import emit

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
                await emit(payload)
        except asyncio.CancelledError:
            raise
        except Exception as e:
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
          (parallel to ``workers/vectorize.py::start_vectorize_consumer``)
          so lane isolation + TTL fallback behave exactly like legacy
          consumers;
        * declare is idempotent (``durable=True, auto_delete=False,
          passive=False``) — if lark-server or ``declare_topology``
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

        from pydantic import ValidationError as _ValidationError

        from app.api.middleware import lane_var, trace_id_var
        from app.infra.rabbitmq import current_lane, lane_queue, mq
        from app.runtime.node import inputs_of

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
        try:
            # Passive fetch: the queue is owned by whoever publishes to
            # it (lark-server for ``vectorize``, ``declare_topology`` for
            # the static ALL_ROUTES set). Trying to re-declare with our
            # own args would clash on DLX / TTL settings — see
            # ``_build_queue_args`` in ``app/infra/rabbitmq.py``. Mirror
            # ``mq.consume``'s ``get_queue`` for exactly that reason.
            queue = await channel.get_queue(actual_queue)
        except Exception as e:
            # Queue missing at consume time is a deployment-level bug
            # (topology / publisher ordering). Fail fast so PaaS restarts
            # us and the operator sees the real cause.
            self._record_source_error(name, e)
            try:
                await channel.close()
            except Exception:  # pragma: no cover
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
                    # Restore trace/lane contextvars from headers, with
                    # the same defensive coercion as durable.py (header
                    # values can legally be bytes / list / int when a
                    # publisher misbehaves — we want "not set" rather
                    # than a cryptic ``str()`` crash downstream).
                    headers = incoming.headers or {}
                    raw_trace = headers.get("trace_id")
                    trace_id = (
                        raw_trace
                        if isinstance(raw_trace, str) and raw_trace
                        else None
                    )
                    raw_lane = headers.get("lane")
                    lane = (
                        raw_lane
                        if isinstance(raw_lane, str) and raw_lane
                        else None
                    )
                    t_tok = trace_id_var.set(trace_id)
                    l_tok = lane_var.set(lane)
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
                            try:
                                body = _json.loads(incoming.body.decode())
                                # MQSource adapts external producers whose
                                # payloads may carry fields meant for other
                                # consumers (e.g. lark-server adds 'lane' to
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
                                # Bad frame: log + ack (continue out of
                                # process() which ack-s on clean exit).
                                # requeue=False already rules out poison
                                # loops; this keeps the loop alive for
                                # the next message.
                                logger.warning(
                                    "mq source %s decode failed: %s body=%r",
                                    actual_queue,
                                    e,
                                    incoming.body[:200],
                                )
                                continue
                            await target(**{param_name: req})
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        # Business-layer failure surfaced through
                        # ``process(requeue=False)`` -> DLX. The loop
                        # must stay alive to drain subsequent messages,
                        # so we log and move on instead of tripping
                        # _record_source_error (which would kill the
                        # pod for a single bad message).
                        logger.exception(
                            "mq source %s: target %s raised %r on one "
                            "message; DLX'd, continuing",
                            actual_queue,
                            target.__name__,
                            e,
                        )
                    finally:
                        trace_id_var.reset(t_tok)
                        lane_var.reset(l_tok)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Only infrastructure errors (channel/connection death,
            # consume-iterator setup failure) reach here — those *are*
            # fatal and should restart the pod.
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
                pass
