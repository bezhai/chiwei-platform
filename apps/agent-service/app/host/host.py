"""The plugin host: starts an app's plugins in dependency order, stops them in the shutdown order,
and takes back everything they registered.

**Setup only registers.** Each plugin's ``setup`` gets a :class:`Context` and registers through
it: clocks, tasks, admin routes, inboxes, the durable edge, outbound queues, services, things to
undo at stop. Every registration returns a disposer that takes it back; :meth:`Host.stop` calls
them all, plugins in reverse order, each plugin's in reverse order of registration. Registering
outside setup is refused: the host has nothing that would start a late registration.

**Start** runs these phases, each only when its flag asks for it:

1. Dynamic Config reads the deployment lane.
2. Every plugin's ``setup``, in order; right after each one, what it provided must match its
   ``provides``.
3. The dataflow graph is reset and compiled, so a mis-wired durable or outbound edge fails here.
   Nothing so far touched the database or the broker: a manifest the host cannot run fails
   before any IO.
4. ``schema``: the business tables of coe lanes (:func:`app.data.bootstrap.ensure_business_schema`).
5. ``mq``: the durable routes are declared, so this process can publish before their consumer runs.
6. ``schema``: the Data tables and the runtime's own (:func:`app.runtime.migrator.migrate_schema`).
7. ``tasks``: every task starts.
8. ``mq``: the durable consumers, then messaging (inboxes, the at-start openers, scheduled delivery).
9. ``http``: the routes go onto the app.
10. ``clocks``: the clocks start, outside prod only with ``DATAFLOW_ENABLE_TIME_SOURCES=1``.

A phase that fails stops the phases already started, takes back every registration, and the
error is raised again.

**Stop** keeps today's shutdown order: clocks first (their ticks still running are cancelled,
rounds included), then the messaging drain, the durable consumers, tasks, the broker connection;
registrations last. A stop phase that fails is logged and the rest still run; the first error is
raised at the end.

**What T1 does not do yet.** Inboxes and the durable consumer start and stop as a whole with
messaging and the durable layer (:mod:`app.messaging`, :mod:`app.runtime.durable`): their
disposers remove the registration, the consumers stop at the host's stop. There is no API that
stops one plugin while the others keep running; plugins are not started or stopped at runtime.
Data classes register themselves when they are imported (``DATA_REGISTRY``) and cannot be taken
back until storage becomes a host service (T2).
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from fastapi import FastAPI
from inner_shared.dynamic_config import dynamic_config

from app import deployment
from app.data.bootstrap import ensure_business_schema
from app.host.errors import (
    DuplicateService,
    HostError,
    MissingService,
    ServiceCycle,
    UndeclaredService,
    UnknownApp,
)
from app.host.http import METHODS, Handler, RouteSpec, bind_route, unbind_route
from app.host.plugin import Disposer, Plugin, Registration
from app.infra.rabbitmq import mq
from app.messaging import receiving
from app.messaging.lifecycle import start_messaging, stop_messaging
from app.runtime.bootstrap import declare_durable_topology
from app.runtime.clock import Clock, Clocks, Tick
from app.runtime.data import Data
from app.runtime.durable import start_consumers, stop_consumers
from app.runtime.emit import reset_emit_runtime
from app.runtime.graph import compile_graph
from app.runtime.lane_policy import (
    current_deployment_lane,
    time_sources_enabled_by_default,
)
from app.runtime.migrator import migrate_schema
from app.runtime.sink import Sink
from app.runtime.wire import WIRING_REGISTRY, WireSpec

logger = logging.getLogger(__name__)


@dataclass(eq=False)
class _Entry:
    """One live registration: what :meth:`Host.registered` shows, and how to take it back."""

    plugin: str
    kind: str
    name: str
    detail: Callable[[], Mapping[str, Any]]
    dispose: Disposer


class Context:
    """What one plugin's ``setup`` registers through. Registration is open only during setup."""

    def __init__(self, host: Host, plugin: Plugin) -> None:
        self._host = host
        self._plugin = plugin
        self._open = True
        self._provided: list[str] = []

    @property
    def app_name(self) -> str:
        return self._host.app_name

    def clock(self, name: str, seconds: float, tick: Tick) -> Disposer:
        """Every ``seconds``, call ``tick(ts)`` in the clock loop and run what it returns.

        ``tick`` should build its work (the payload) before returning it, so a payload that
        cannot be built stops the process instead of failing quietly. See :mod:`app.runtime.clock`.
        """
        if seconds <= 0:
            raise ValueError(f"clock {name!r}: seconds must be positive, got {seconds!r}")
        clock = Clock(name=f"clock[{name}]", seconds=float(seconds), tick=tick)
        host = self._host

        async def undo() -> None:
            host._clock_specs.pop(name, None)
            if host._clocks is not None:
                await host._clocks.remove(clock.name)

        return self._register(
            "clock",
            name,
            lambda: {"seconds": clock.seconds, "tick": tick},
            undo,
            add=lambda: host._clock_specs.__setitem__(name, clock),
        )

    def task(self, name: str, run: Callable[[], Awaitable[None]]) -> Disposer:
        """A background task, started with ``run()`` in the tasks phase, cancelled at stop."""
        host = self._host

        async def undo() -> None:
            host._task_runs.pop(name, None)
            task = host._tasks.pop(name, None)
            if task is not None:
                await _cancel(task)

        return self._register(
            "task",
            name,
            lambda: {"run": run},
            undo,
            add=lambda: host._task_runs.__setitem__(name, run),
        )

    def route(
        self,
        method: str,
        path: str,
        request: type[Data],
        handler: Handler,
        *,
        inner_secret: bool = False,
        lane_match: bool = False,
        answers_with_lane: bool = False,
    ) -> Disposer:
        """An admin route: the request becomes ``request``, ``handler``'s answer is the body.

        ``inner_secret``: ``Authorization: Bearer <INNER_HTTP_SECRET>`` is required.
        ``lane_match``: a request meant for another lane (``x-ctx-lane``) is refused with 409.
        ``answers_with_lane``: the refusals the host gives carry this process's lane.
        See :mod:`app.runtime.http_auth`.
        """
        method = method.upper()
        if method not in METHODS:
            raise ValueError(f"unsupported HTTP method {method!r} (supported: {', '.join(METHODS)})")
        spec = RouteSpec(
            method=method,
            path=path,
            request=request,
            handler=handler,
            inner_secret=inner_secret,
            lane_match=lane_match,
            answers_with_lane=answers_with_lane,
        )
        key = f"{method} {path}"
        host = self._host

        async def undo() -> None:
            host._routes.pop(key, None)
            bound = host._bound.pop(key, None)
            if bound is not None:
                unbind_route(host._http, bound)

        return self._register(
            "route",
            key,
            lambda: {
                "method": method,
                "path": path,
                "request": request,
                "handler": handler,
                "inner_secret": inner_secret,
                "lane_match": lane_match,
                "answers_with_lane": answers_with_lane,
            },
            undo,
            add=lambda: host._routes.__setitem__(key, spec),
        )

    def inbox(
        self,
        name: str,
        *,
        on_message: receiving.OnMessage,
        on_question: receiving.OnQuestion | None = None,
        processing_timeout: timedelta | None = None,
        on_open: receiving.OnOpen | None = None,
        consume_while: receiving.ConsumeWhile | None = None,
        retry_without_limit: receiving.RetryWithoutLimit | None = None,
    ) -> Disposer:
        """Own the inbox ``name``; see :func:`app.messaging.receiving.inbox` for the options."""
        options = {
            "on_message": on_message,
            "on_question": on_question,
            "processing_timeout": processing_timeout,
            "on_open": on_open,
            "consume_while": consume_while,
            "retry_without_limit": retry_without_limit,
        }
        declared: list[receiving.InboxSpec] = []

        def add() -> None:
            receiving.inbox(name, **options)
            declared.append(receiving.INBOX_REGISTRY[name])

        async def undo() -> None:
            if declared and receiving.INBOX_REGISTRY.get(name) is declared[0]:
                del receiving.INBOX_REGISTRY[name]

        return self._register("inbox", name, lambda: dict(options), undo, add=add)

    def inboxes_at_start(self, open_them: receiving.OpenAtStart) -> Disposer:
        """Inboxes whose names are known only at start; see
        :func:`app.messaging.receiving.inboxes_at_start`. The host notes which inboxes
        ``open_them`` declared, so taking it back takes those back too."""
        declared: list[str] = []

        async def opener() -> None:
            before = set(receiving.INBOX_REGISTRY)
            try:
                await open_them()
            finally:
                declared.extend(
                    n for n in receiving.INBOX_REGISTRY if n not in before and n not in declared
                )

        async def undo() -> None:
            with suppress(ValueError):
                receiving.INBOXES_AT_START.remove(opener)
            for name in declared:
                receiving.INBOX_REGISTRY.pop(name, None)
            declared.clear()

        return self._register(
            "inboxes_at_start",
            getattr(open_them, "__qualname__", repr(open_them)),
            lambda: {"open": open_them, "declared": tuple(declared)},
            undo,
            add=lambda: receiving.inboxes_at_start(opener),
        )

    def durable(self, data_type: type[Data], consumer: Callable) -> Disposer:
        """The durable edge ``data_type`` → ``consumer`` (a ``@node``): an emit of ``data_type``
        is published to the consumer's queue and consumed by :mod:`app.runtime.durable`."""
        spec = WireSpec(data_type=data_type, consumers=[consumer], durable=True)
        return self._register(
            "durable",
            f"{data_type.__name__}->{consumer.__name__}",
            lambda: {"data_type": data_type, "consumer": consumer},
            _unwire(spec),
            add=lambda: WIRING_REGISTRY.append(spec),
        )

    def outbound(self, data_type: type[Data], queue: str) -> Disposer:
        """An emit of ``data_type`` is published to the outbound ``queue`` (``Sink.mq``)."""
        spec = WireSpec(data_type=data_type, sinks=[Sink.mq(queue)])
        return self._register(
            "outbound",
            f"{data_type.__name__}->{queue}",
            lambda: {"data_type": data_type, "queue": queue},
            _unwire(spec),
            add=lambda: WIRING_REGISTRY.append(spec),
        )

    def provide(self, name: str, service: object) -> None:
        """Hand over the service ``name``; it must be listed in this plugin's ``provides``."""
        host = self._host
        self._register(
            "service",
            name,
            dict,
            _async(lambda: host._services.pop(name, None)),
            add=lambda: host._services.__setitem__(name, service),
        )
        self._provided.append(name)

    def service(self, name: str) -> object:
        """A service some plugin provides; only names listed in this plugin's ``requires``."""
        if name not in self._plugin.requires:
            raise UndeclaredService(
                f"app {self.app_name!r}: plugin {self._plugin.name!r} asked for service "
                f"{name!r}, which it does not list in requires"
            )
        try:
            return self._host._services[name]
        except KeyError:
            raise HostError(
                f"app {self.app_name!r}: service {name!r} is not available; the host is not running"
            ) from None

    def on_stop(self, undo: Callable[[], Awaitable[None] | None]) -> None:
        """Run ``undo`` at stop, with this plugin's other registrations (it may be async)."""

        async def run() -> None:
            result = undo()
            if inspect.isawaitable(result):
                await result

        self._register("on_stop", getattr(undo, "__qualname__", repr(undo)), dict, run)

    # ------------------------------------------------------------------

    def _register(
        self,
        kind: str,
        name: str,
        detail: Callable[[], Mapping[str, Any]],
        undo: Callable[[], Awaitable[None]],
        *,
        add: Callable[[], None] = lambda: None,
    ) -> Disposer:
        if not self._open:
            raise HostError(
                f"app {self.app_name!r}: plugin {self._plugin.name!r} tried to register "
                f"{kind} {name!r} after its setup; registration is open only during setup"
            )
        self._host._check_unique(self._plugin.name, kind, name)
        add()
        entries = self._host._entries

        async def dispose() -> None:
            if entry not in entries:
                return
            entries.remove(entry)
            await undo()

        entry = _Entry(self._plugin.name, kind, name, detail, dispose)
        entries.append(entry)
        return dispose


class Host:
    """The plugins of one app, started and stopped together. See the module docstring."""

    def __init__(self, app_name: str, plugins: Sequence[Plugin]) -> None:
        self._app_name = app_name
        self._plugins = _resolve(app_name, list(plugins))
        self._entries: list[_Entry] = []
        self._services: dict[str, object] = {}
        self._clock_specs: dict[str, Clock] = {}
        self._task_runs: dict[str, Callable[[], Awaitable[None]]] = {}
        self._routes: dict[str, RouteSpec] = {}
        self._phases: set[str] = set()
        self._http: FastAPI | None = None
        self._bound: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._clocks: Clocks | None = None

    @classmethod
    def for_app(cls, app_name: str) -> Host:
        """The host for ``app_name`` as ``app.deployment.APPS`` lists it: each module's ``PLUGIN``."""
        modules = deployment.APPS.get(app_name)
        if modules is None:
            raise UnknownApp(
                f"app {app_name!r} is not declared in app.deployment.APPS "
                f"(declared: {sorted(deployment.APPS)})"
            )
        return cls(app_name, [importlib.import_module(m).PLUGIN for m in modules])

    @property
    def app_name(self) -> str:
        return self._app_name

    @property
    def plugins(self) -> tuple[Plugin, ...]:
        """The plugins in the order they are set up."""
        return self._plugins

    def registered(self) -> tuple[Registration, ...]:
        """Everything the plugins registered and nothing has taken back, in registration order."""
        return tuple(
            Registration(e.plugin, e.kind, e.name, dict(e.detail())) for e in self._entries
        )

    async def start(
        self, *, http: FastAPI | None, schema: bool, mq: bool, clocks: bool, tasks: bool
    ) -> None:
        if self._phases:
            raise HostError(f"app {self._app_name!r}: the host is already started; stop it first")
        try:
            await self._start(http=http, schema=schema, use_mq=mq, clocks=clocks, tasks=tasks)
        except BaseException:
            try:
                await self.stop()
            except Exception:
                logger.exception(
                    "host: app %s: cleaning up after the failed start failed too", self._app_name
                )
            raise

    async def stop(self) -> None:
        phases, self._phases = self._phases, set()
        if not phases:
            return
        errors: list[Exception] = []

        async def step(what: str, run: Callable[[], Awaitable[None]]) -> None:
            try:
                await run()
            except Exception as e:
                logger.exception("host: app %s: stopping %s failed", self._app_name, what)
                errors.append(e)

        if "clocks" in phases:
            await step("clocks", self._stop_clocks)
        if "mq" in phases:
            await step("messaging", stop_messaging)
            await step("durable consumers", stop_consumers)
        if "tasks" in phases:
            await step("tasks", self._stop_tasks)
        if "mq" in phases:
            await step("the broker connection", mq.close)
        await step("registrations", self._revoke)
        self._http = None
        if errors:
            raise errors[0]

    # ------------------------------------------------------------------

    async def _start(
        self, *, http: FastAPI | None, schema: bool, use_mq: bool, clocks: bool, tasks: bool
    ) -> None:
        self._phases.add("setup")
        dynamic_config.set_lane_provider(current_deployment_lane)
        for plugin in self._plugins:
            self._set_up(plugin)
        reset_emit_runtime()
        compile_graph(self._app_name)

        if schema:
            await ensure_business_schema()
        if use_mq:
            self._phases.add("mq")
            await declare_durable_topology()
        if schema:
            await migrate_schema()
        if tasks:
            self._phases.add("tasks")
            for name, run in self._task_runs.items():
                task = asyncio.create_task(run(), name=f"task[{name}]")
                task.add_done_callback(self._task_ended)
                self._tasks[name] = task
        if use_mq:
            await start_consumers()
            await start_messaging()
        if http is not None:
            self._phases.add("http")
            self._http = http
            for key, spec in self._routes.items():
                self._bound[key] = bind_route(http, spec)
        if clocks:
            self._phases.add("clocks")
            self._clocks = Clocks(self._app_name)
            await self._clocks.start(
                self._clock_specs.values(), enabled=time_sources_enabled_by_default()
            )

    def _set_up(self, plugin: Plugin) -> None:
        ctx = Context(self, plugin)
        try:
            result = plugin.setup(ctx)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise HostError(
                    f"app {self._app_name!r}: plugin {plugin.name!r}: setup must be synchronous "
                    f"(registration only, no IO)"
                )
        finally:
            ctx._open = False
        declared, provided = set(plugin.provides), set(ctx._provided)
        problems = [
            f"plugin {plugin.name!r} provided {name!r} without declaring it in provides"
            for name in sorted(provided - declared)
        ] + [
            f"plugin {plugin.name!r} did not provide {name!r}, which it declares in provides"
            for name in sorted(declared - provided)
        ]
        if problems:
            raise UndeclaredService(f"app {self._app_name!r}: " + "; ".join(problems))

    def _check_unique(self, plugin: str, kind: str, name: str) -> None:
        if kind == "on_stop":
            return
        for e in self._entries:
            if e.kind == kind and e.name == name:
                if kind == "service":
                    raise DuplicateService(
                        f"app {self._app_name!r}: service {name!r} is provided by both "
                        f"{e.plugin!r} and {plugin!r}"
                    )
                raise HostError(
                    f"app {self._app_name!r}: {kind} {name!r} is already registered by "
                    f"plugin {e.plugin!r}"
                )

    def _task_ended(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "host: app %s: %s died", self._app_name, task.get_name(), exc_info=error
            )
        else:
            logger.warning("host: app %s: %s returned on its own", self._app_name, task.get_name())

    async def _stop_clocks(self) -> None:
        clocks, self._clocks = self._clocks, None
        if clocks is not None:
            await clocks.stop()

    async def _stop_tasks(self) -> None:
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            await _cancel(task)

    async def _revoke(self) -> None:
        errors: list[Exception] = []
        for entry in reversed(list(self._entries)):
            try:
                await entry.dispose()
            except Exception as e:
                logger.exception(
                    "host: app %s: taking back %s %r of plugin %s failed",
                    self._app_name,
                    entry.kind,
                    entry.name,
                    entry.plugin,
                )
                errors.append(e)
        reset_emit_runtime()
        if errors:
            raise errors[0]


def _resolve(app: str, plugins: list[Plugin]) -> tuple[Plugin, ...]:
    """Plugins in setup order: after the plugins whose services they require, otherwise in
    manifest order. Refuses duplicates, missing services and cycles."""
    names = [p.name for p in plugins]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise HostError(
            f"app {app!r}: plugin(s) {', '.join(map(repr, repeated))} appear more than once "
            f"in the manifest"
        )

    providers: dict[str, list[str]] = {}
    for p in plugins:
        for service in p.provides:
            providers.setdefault(service, []).append(p.name)
    shared = {s: ps for s, ps in providers.items() if len(ps) > 1}
    if shared:
        raise DuplicateService(
            f"app {app!r}: "
            + "; ".join(
                f"service {s!r} is provided by {', '.join(map(repr, ps))}"
                for s, ps in sorted(shared.items())
            )
        )

    missing = [(p.name, s) for p in plugins for s in p.requires if s not in providers]
    if missing:
        provided = ", ".join(map(repr, sorted(providers))) or "nothing"
        raise MissingService(
            "\n".join(
                f"app {app!r}: plugin {p!r} requires {s!r}; nothing in the manifest provides it "
                f"(provided: {provided})"
                for p, s in missing
            )
        )

    needs = {p.name: {providers[s][0] for s in p.requires} for p in plugins}
    order: list[Plugin] = []
    done: set[str] = set()
    while len(order) < len(plugins):
        ready = [p for p in plugins if p.name not in done and needs[p.name] <= done]
        if not ready:
            break
        order.append(ready[0])
        done.add(ready[0].name)
    if len(order) < len(plugins):
        raise ServiceCycle(f"app {app!r}: " + "; ".join(_cycles(plugins, needs, done)))
    return tuple(order)


def _cycles(plugins: list[Plugin], needs: dict[str, set[str]], done: set[str]) -> list[str]:
    """Each cycle among the plugins that could not be ordered, as ``'a' -> 'b' -> 'a'``."""
    position = {p.name: i for i, p in enumerate(plugins)}
    stuck = [p.name for p in plugins if p.name not in done]
    seen: list[frozenset[str]] = []
    found: list[str] = []
    for start in stuck:
        path = [start]
        while True:
            nxt = min((n for n in needs[path[-1]] if n not in done), key=position.__getitem__)
            if nxt in path:
                cycle = path[path.index(nxt) :]
                break
            path.append(nxt)
        if frozenset(cycle) in seen:
            continue
        seen.append(frozenset(cycle))
        found.append(
            "plugins require each other's services, so none of them can be set up first: "
            + " -> ".join(map(repr, [*cycle, cycle[0]]))
        )
    return found


def _unwire(spec: WireSpec) -> Callable[[], Awaitable[None]]:
    async def undo() -> None:
        for i, w in enumerate(WIRING_REGISTRY):
            if w is spec:
                del WIRING_REGISTRY[i]
                break
        reset_emit_runtime()

    return undo


def _async(fn: Callable[[], object]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        fn()

    return run


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as e:
        # Classification: HARMLESS teardown: the task's own error is logged by _task_ended or
        # here, and must not stop the rest of the stop.
        logger.warning("host: %s exited with %r", task.get_name(), e)
