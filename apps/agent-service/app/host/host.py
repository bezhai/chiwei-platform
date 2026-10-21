"""The plugin host: starts an app's plugins in dependency order, stops them in the shutdown order,
and takes back everything they registered.

**Setup only registers.** Each plugin's ``setup`` gets a :class:`Context` and registers through
it: queues of turns and the clocks that signal them, clocks, tasks, admin routes, inboxes,
outbound queues, services, things to undo at stop, and what a round is made of (tools, segments,
state and input sections, the steps before and after the model, the round log), which the
manifest's profiles and agents place (``ctx.profile`` gives a plugin the profile its rounds run
from, ``ctx.participants`` whose rounds run from it, ``ctx.agent`` an agent it calls with the tools
the manifest lists for it).
Every registration returns a disposer that takes it back; :meth:`Host.stop` calls them all,
plugins in reverse order, each plugin's in reverse order of registration. Registering outside
setup is refused: the host has nothing that would start a late registration.

**Start** runs these phases, each only when its flag asks for it:

1. Dynamic Config reads the deployment lane.
2. Every plugin's ``setup``, in order; right after each one, what it provided must match its
   ``provides``. Then the manifest is checked against what the plugins registered for the rounds
   (tools, segments, sections, steps) and the profiles and agents are filled in
   (:mod:`app.host.assembly`).
3. Nothing so far touched the database or the broker: a manifest the host cannot run, or an
   outbound queue no route declares (checked when ``ctx.outbound`` registers it), fails before
   any IO.
4. ``schema``: the business tables of coe lanes (:func:`app.data.bootstrap.ensure_business_schema`).
5. ``mq``: the broker connection and its main exchange, which the outbound queues publish on.
6. ``schema``: the Data tables (:func:`app.data.migrator.migrate_schema`), and messaging's own
   tables (claims, dead-letter audit), whose DDL the host passes in.
7. ``tasks``: every task starts.
8. ``mq``: messaging (inboxes, the at-start openers, scheduled delivery).
9. ``http``: the routes go onto the app.
10. ``clocks``: the clocks start, the ones signalling queues of turns (``ctx.wake``) included,
    outside prod only with ``DATAFLOW_ENABLE_TIME_SOURCES=1``.

A phase that fails stops the phases already started, takes back every registration, and the
error is raised again.

**Stop** keeps today's shutdown order: clocks first (their ticks still running are cancelled,
the turns their wakes run included, with no grace), then the messaging drain, tasks, the broker
connection; registrations last. A stop phase that fails is logged and the rest still run; the
first error is raised at the end. A stop that is cancelled raises the cancellation and leaves
what it had not reached to the next stop; the piece it interrupted is not run again.

**What T1 does not do yet.** Inboxes start and stop as a whole with messaging
(:mod:`app.messaging`): their disposers remove the registration, the consumers stop at the host's
stop. There is no API that stops one plugin while the others keep running; plugins are not
started or stopped at runtime.
Data classes register themselves when they are imported (``DATA_REGISTRY`` in
:mod:`app.data.schema`), so the tables an app builds are those of the code it loads; tables are
never dropped, so there is nothing to take back.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import FastAPI
from inner_shared.dynamic_config import dynamic_config

from app import deployment
from app.agent.contracts import Continue, FailRound, Segment, ToolSpec, Trim
from app.agent.round import (
    AfterCommit,
    AssembledAgent,
    AssembledProfile,
    Round,
    RoundLog,
    Section,
    Step,
)
from app.agent.tooling import Tool
from app.data.bootstrap import ensure_business_schema
from app.data.migrator import migrate_schema
from app.data.schema import Data
from app.host.assembly import KINDS, Contribution, assemble
from app.host.clock import Clock, Clocks, Tick
from app.host.errors import (
    DuplicateService,
    HostError,
    MissingService,
    ServiceCycle,
    UndeclaredService,
    UnknownApp,
)
from app.host.http import METHODS, Handler, RouteSpec, bind_route, unbind_route
from app.host.manifest import App
from app.host.plugin import Disposer, Plugin, Registration
from app.host.turns import Signal, Turns, WakeSource
from app.infra import outbound
from app.infra.cst_time import now_cst
from app.infra.lane_policy import (
    current_deployment_lane,
    time_sources_enabled_by_default,
)
from app.infra.rabbitmq import mq
from app.messaging import receiving
from app.messaging.claims import RUNTIME_INFLIGHT_DDL
from app.messaging.dead_letter_audit import RUNTIME_DLQ_AUDIT_DDL
from app.messaging.lifecycle import start_messaging, stop_messaging

logger = logging.getLogger(__name__)


class Inbox:
    """An inbox a plugin owns, as :meth:`Context.inbox` hands it back. Called, it takes the
    registration back, like every disposer; it also answers what the owner asks messaging about
    this inbox."""

    def __init__(self, name: str, max_attempts: int | None, dispose: Disposer) -> None:
        self.name = name
        self._max_attempts = max_attempts
        self._dispose = dispose

    async def __call__(self) -> None:
        await self._dispose()

    async def succeeded(self, message_ids: Iterable[str]) -> set[str]:
        """Which of ``message_ids`` had a delivery to this inbox handled and its success recorded
        (:func:`app.messaging.receiving.succeeded_message_ids`): an owner that keeps its own record
        of what it handled forgets these."""
        return await receiving.succeeded_message_ids(self.name, message_ids)

    @property
    def attempt_limit(self) -> int:
        """How many times messaging hands one message to this inbox's handler at most, the first
        time included, before it goes to the dead letters (the inbox's ``max_attempts``, or
        messaging's default; read when asked). A message the inbox's ``retry_without_limit``
        exempts has no limit."""
        return receiving.processing_retry(self._max_attempts).n


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

    def turns(
        self, participant: str, line: str = "round", *, hold_limit: timedelta | None = None
    ) -> Turns:
        """A queue of turns for ``participant`` on ``line`` (:mod:`app.host.turns`), new each time
        the host starts. One participant has one queue per line. The queue hands its wake sources
        the host's clock (Beijing time) to read when they decide on a round."""
        turns = Turns(participant, line, hold_limit=hold_limit, clock=now_cst)
        self._register("turns", f"{participant}/{line}", lambda: {"turns": turns}, _nothing)
        return turns

    def wake(self, turns: Turns, source: WakeSource, *, every: timedelta) -> Disposer:
        """Every ``every``, signal ``turns`` for ``source`` with ``Signal(at=<the tick's time>)``.

        Registrations of one source name with one interval share one clock: each tick signals
        every queue registered for it at once, and one queue's error is logged as a warning
        without keeping the others from their turn. The turns run in the clock's work, so a stop
        cancels them first, with no grace (:meth:`Host.stop`). Like every clock, it runs outside
        prod only with ``DATAFLOW_ENABLE_TIME_SOURCES=1``, and its first tick comes one interval
        after the clock starts. A source driven by deliveries does not register here: its inbox
        handler calls ``turns.take`` itself.
        """
        seconds = every.total_seconds()
        if seconds <= 0:
            raise ValueError(f"wake {source.name!r}: every must be positive, got {every!r}")
        host = self._host
        key = (source.name, seconds)
        target = (turns, source)

        def add() -> None:
            host._wakes.setdefault(key, []).append(target)

        async def undo() -> None:
            targets = host._wakes.get(key, [])
            for i, registered in enumerate(targets):
                if registered is target:
                    del targets[i]
                    break
            if not targets:
                host._wakes.pop(key, None)
                if host._clocks is not None:
                    await host._clocks.remove(_wake_clock_name(*key))

        return self._register(
            "wake",
            f"{source.name} -> {turns.participant}/{turns.line}",
            lambda: {"turns": turns, "source": source, "every": every},
            undo,
            add=add,
        )

    def clock(self, name: str, seconds: float, tick: Tick) -> Disposer:
        """Every ``seconds``, call ``tick(ts)`` in the clock loop and run what it returns.

        For periodic background work that puts no participant's round on a queue (matching what
        she said against what landed); a source of a participant's rounds registers with
        :meth:`wake`. ``tick`` should build its work (the payload) before returning it, so a
        payload that cannot be built stops the process instead of failing quietly. See
        :mod:`app.host.clock`.
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
        See :mod:`app.host.http_auth`.
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
        max_attempts: int | None = None,
    ) -> Inbox:
        """Own the inbox ``name``; see :func:`app.messaging.receiving.inbox` for the options. The
        :class:`Inbox` it returns takes the registration back when called."""
        options = {
            "on_message": on_message,
            "on_question": on_question,
            "processing_timeout": processing_timeout,
            "on_open": on_open,
            "consume_while": consume_while,
            "retry_without_limit": retry_without_limit,
            "max_attempts": max_attempts,
        }
        declared: list[receiving.InboxSpec] = []

        def add() -> None:
            receiving.inbox(name, **options)
            declared.append(receiving.INBOX_REGISTRY[name])

        async def undo() -> None:
            if declared and receiving.INBOX_REGISTRY.get(name) is declared[0]:
                del receiving.INBOX_REGISTRY[name]

        dispose = self._register("inbox", name, lambda: dict(options), undo, add=add)
        return Inbox(name, max_attempts, dispose)

    def inboxes_at_start(self, open_them: receiving.OpenAtStart) -> Disposer:
        """Inboxes whose names are known only at start; see
        :func:`app.messaging.receiving.inboxes_at_start`. ``open_them`` is handed a ``declare``
        function (the options of :meth:`inbox`) and declares its inboxes through it. The host
        notes every name declared that way, so taking ``open_them`` back takes those back too."""
        declared: list[str] = []

        async def opener(declare: receiving.DeclareInbox) -> None:
            def noted(name: str, **options: Any) -> None:
                declare(name, **options)
                if name not in declared:
                    declared.append(name)

            await open_them(noted)

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

    def tool(self, tool: Tool, *, trim: Trim, ends_round: bool = False) -> Disposer:
        """Declare ``tool``: ``trim`` says how trimming treats what it returns (``"material"`` is
        replaced with a short line once it is older than the history policy's material minutes,
        ``"kept"`` stays as long as the agent's own words), ``ends_round`` whether calling it ends
        the round. One tool name per app. The declaration is a
        :class:`~app.agent.contracts.ToolSpec`; the schema the model sees is reflected from
        ``tool`` unchanged."""
        spec = ToolSpec(tool, trim=trim, ends_round=ends_round)
        return self._contribute("tool", tool.name, spec, {"spec": spec})

    def segment(self, segment: Segment) -> Disposer:
        """Contribute ``segment`` (one Langfuse prompt of an agent's prompt) under its name; the
        manifest's agents list it among their segments."""
        return self._contribute("segment", segment.name, segment, {"segment": segment})

    def section(self, name: str, section: Section) -> Disposer:
        """A state or an input section of a round: ``section(round)`` gives its text, or ``None``
        to leave it out. The manifest's profile places it among the state or the input."""
        return self._contribute("section", name, section, {"section": section})

    def round_start(self, name: str, step: Step) -> Disposer:
        """A step before the round, before its history is read; it failing fails the round."""
        return self._contribute("round_start", name, step, {"step": step})

    def check(self, name: str, step: Step) -> Disposer:
        """A check after the model, before the commit; it failing fails the round, and nothing
        is committed."""
        return self._contribute("check", name, step, {"step": step})

    def commit(self, name: str, step: Step) -> Disposer:
        """A step of the commit: every commit step of a round runs in one transaction."""
        return self._contribute("commit", name, step, {"step": step})

    def after_commit(
        self, name: str, step: Step, *, on_failure: FailRound | Continue
    ) -> Disposer:
        """A step after the commit. ``on_failure`` says what it failing does: ``FailRound()``
        fails the round, ``Continue()`` is logged and counted and the round goes on."""
        after = AfterCommit(step, on_failure)
        return self._contribute(
            "after_commit", name, after, {"step": step, "on_failure": on_failure}
        )

    def round_log(
        self,
        name: str,
        *,
        previous_at: Callable[[Round], Awaitable[datetime | None]],
        lost_last_round: Callable[[Round, int], Awaitable[bool]],
    ) -> Disposer:
        """How a profile's rounds read the round before (:class:`app.agent.round.RoundLog`); a
        profile lists at most one."""
        log = RoundLog(previous_at, lost_last_round)
        return self._contribute(
            "round_log",
            name,
            log,
            {"previous_at": previous_at, "lost_last_round": lost_last_round},
        )

    def profile(self, name: str) -> AssembledProfile:
        """The manifest's profile ``name``, as the host fills it in once every plugin has
        registered (before any IO): what the plugin's wake source hands to
        :func:`app.agent.round.run_round`."""
        try:
            return self._host._profiles[name]
        except KeyError:
            raise HostError(
                f"app {self.app_name!r}: plugin {self._plugin.name!r} asked for profile "
                f"{name!r}, which the manifest does not list "
                f"(listed: {sorted(self._host._profiles)})"
            ) from None

    def participants(self, *, profile: str) -> tuple[str, ...]:
        """The names of the manifest's participants whose rounds run from ``profile``, in the
        order the manifest lists them: who the plugin registers queues of turns and inboxes for."""
        if profile not in self._host._manifest.profiles:
            raise HostError(
                f"app {self.app_name!r}: plugin {self._plugin.name!r} asked for the participants "
                f"of profile {profile!r}, which the manifest does not list "
                f"(listed: {sorted(self._host._manifest.profiles)})"
            )
        return tuple(
            p.name for p in self._host._manifest.participants if p.profile == profile
        )

    def agent(self, name: str) -> AssembledAgent:
        """The manifest's agent ``name``, with the tools its spec lists as the host fills them in
        once every plugin has registered (before any IO): what the plugin calls it with, leaving out
        the tools of a plugin that is switched off."""
        try:
            return self._host._agents[name]
        except KeyError:
            raise HostError(
                f"app {self.app_name!r}: plugin {self._plugin.name!r} asked for agent "
                f"{name!r}, which the manifest does not list "
                f"(listed: {sorted(self._host._agents)})"
            ) from None

    def outbound(self, data_type: type[Data], queue: str) -> Disposer:
        """A :func:`app.infra.outbound.publish` of a ``data_type`` goes to the outbound ``queue``.
        A queue no route declares, a queue partitioned by channel for a type with no ``channel``,
        or a type that already goes to a queue is refused here, in setup."""
        taken_back: list[Callable[[], None]] = []
        return self._register(
            "outbound",
            f"{data_type.__name__}->{queue}",
            lambda: {"data_type": data_type, "queue": queue},
            _async(lambda: taken_back.pop()()),
            add=lambda: taken_back.append(outbound.register(data_type, queue)),
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

    def _contribute(
        self, kind: str, name: str, value: object, detail: Mapping[str, Any]
    ) -> Disposer:
        """Register what a manifest places; its name is checked when the host assembles."""
        contribution = Contribution(kind, name, self._plugin.name, value)
        contributed = self._host._contributions
        return self._register(
            kind,
            name,
            lambda: detail,
            _async(lambda: contributed.remove(contribution)),
            add=lambda: contributed.append(contribution),
        )

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

    def __init__(
        self, app_name: str, plugins: Sequence[Plugin], *, manifest: App | None = None
    ) -> None:
        self._app_name = app_name
        self._plugins = _resolve(app_name, list(plugins))
        self._manifest = manifest or App(plugins=(), agents={}, profiles={}, participants=())
        self._profiles = {
            name: AssembledProfile(app=app_name, name=name, profile=profile)
            for name, profile in self._manifest.profiles.items()
        }
        self._agents = {
            name: AssembledAgent(app=app_name, name=name, spec=spec)
            for name, spec in self._manifest.agents.items()
        }
        self._contributions: list[Contribution] = []
        self._entries: list[_Entry] = []
        self._services: dict[str, object] = {}
        self._clock_specs: dict[str, Clock] = {}
        # (source name, seconds) -> the queues one wake clock signals, with the source each is
        # signalled for.
        self._wakes: dict[tuple[str, float], list[tuple[Turns, WakeSource]]] = {}
        self._task_runs: dict[str, Callable[[], Awaitable[None]]] = {}
        self._routes: dict[str, RouteSpec] = {}
        self._phases: set[str] = set()
        self._http: FastAPI | None = None
        self._bound: dict[str, Any] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._clocks: Clocks | None = None

    @classmethod
    def for_app(cls, app_name: str) -> Host:
        """The host for ``app_name``: its manifest (the ``MANIFEST`` of the module
        ``app.deployment.APPS`` names), and the ``PLUGIN`` of each module the manifest lists."""
        module = deployment.APPS.get(app_name)
        if module is None:
            raise UnknownApp(
                f"app {app_name!r} is not declared in app.deployment.APPS "
                f"(declared: {sorted(deployment.APPS)})"
            )
        manifest: App = importlib.import_module(module).MANIFEST
        return cls(
            app_name,
            [importlib.import_module(m).PLUGIN for m in manifest.plugins],
            manifest=manifest,
        )

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
            # A cancellation of this task during the cleanup comes out of stop() and is raised
            # instead, with the start's error as its __context__; the next stop() finishes.
            try:
                await self.stop()
            except Exception:
                logger.exception(
                    "host: app %s: cleaning up after the failed start failed too", self._app_name
                )
            raise

    async def stop(self) -> None:
        """Stop the phases that started, then take back every registration.

        Each piece of cleanup (a phase, a registration) runs at most once. If the task running the
        stop is cancelled, the cancellation is raised at once and what the stop had not reached is
        left to the next ``stop()``; the piece it interrupted is not run again, since it may be
        what hung.
        """
        if not self._phases:
            return
        cleanup = _Cleanup(self._app_name)
        for phase, run in (
            ("clocks", self._stop_clocks),
            ("messaging", stop_messaging),
            ("tasks", self._stop_tasks),
            ("the broker connection", mq.close),
        ):
            if phase in self._phases:
                self._phases.discard(phase)
                await cleanup.run(f"stopping {phase}", run)
        for entry in reversed(list(self._entries)):
            await cleanup.run(
                f"taking back {entry.kind} {entry.name!r} of plugin {entry.plugin}", entry.dispose
            )
        self._phases.clear()
        self._http = None
        if cleanup.errors:
            raise cleanup.errors[0]

    # ------------------------------------------------------------------

    async def _start(
        self, *, http: FastAPI | None, schema: bool, use_mq: bool, clocks: bool, tasks: bool
    ) -> None:
        self._phases.add("setup")
        dynamic_config.set_lane_provider(current_deployment_lane)
        for plugin in self._plugins:
            self._set_up(plugin)
        assemble(
            self._app_name,
            self._manifest,
            self._contributions,
            self._profiles,
            self._agents,
            lane=current_deployment_lane() or "prod",
        )

        if schema:
            await ensure_business_schema()
        if use_mq:
            self._phases.update(("messaging", "the broker connection"))
            await mq.connect()
            await mq.declare_topology()
        if schema:
            await migrate_schema(also=(*RUNTIME_INFLIGHT_DDL, *RUNTIME_DLQ_AUDIT_DDL))
        if tasks:
            self._phases.add("tasks")
            for name, run in self._task_runs.items():
                task = asyncio.create_task(run(), name=f"task[{name}]")
                task.add_done_callback(self._task_ended)
                self._tasks[name] = task
        if use_mq:
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
                [
                    *self._clock_specs.values(),
                    *(_wake_clock(*key, targets) for key, targets in self._wakes.items()),
                ],
                enabled=time_sources_enabled_by_default(),
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
        # What a manifest places is checked when the host assembles, every duplicate at once.
        if kind == "on_stop" or kind in KINDS:
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


class _Cleanup:
    """Runs the pieces of one stop and keeps their failures.

    A piece that fails is logged and noted, and the rest still run. A CancelledError is the stop
    being cancelled only when the task running the stop was cancelled since the stop began
    (``Task.cancelling()`` went up): then it is raised, and raised too when the piece caught it
    and returned, as waiting for a cancelled task does (:func:`_cancel`, the clock runner). A
    CancelledError nothing cancelled the stop for, one a callback raised itself, is a failure like
    any other, noted as a :class:`HostError` so that no caller takes it for its own cancellation.
    """

    def __init__(self, app_name: str) -> None:
        self._app_name = app_name
        self._task = asyncio.current_task()
        self._cancels = self._task.cancelling()
        self.errors: list[Exception] = []

    async def run(self, what: str, piece: Callable[[], Awaitable[None]]) -> None:
        try:
            await piece()
        except asyncio.CancelledError as e:
            if self._cancelled():
                raise
            logger.exception(
                "host: app %s: %s raised CancelledError, but nothing cancelled the stop",
                self._app_name,
                what,
            )
            error = HostError(
                f"app {self._app_name!r}: {what} raised CancelledError, "
                f"but nothing cancelled the stop"
            )
            error.__cause__ = e
            self.errors.append(error)
        except Exception as e:
            logger.exception("host: app %s: %s failed", self._app_name, what)
            self.errors.append(e)
        if self._cancelled():
            raise asyncio.CancelledError

    def _cancelled(self) -> bool:
        return self._task.cancelling() > self._cancels


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


def _wake_clock_name(source_name: str, seconds: float) -> str:
    return f"clock[wake {source_name} every {seconds:g}s]"


def _wake_clock(
    source_name: str, seconds: float, targets: list[tuple[Turns, WakeSource]]
) -> Clock:
    """The one clock of the wakes registered for ``source_name`` every ``seconds``. It reads
    ``targets`` at each tick, so a wake taken back stops being signalled."""

    def tick(ts: datetime) -> Awaitable[None]:
        return _signal_each(list(targets), Signal(at=ts))

    return Clock(name=_wake_clock_name(source_name, seconds), seconds=seconds, tick=tick)


async def _signal_each(targets: list[tuple[Turns, WakeSource]], signal: Signal) -> None:
    """Signal every queue at once; one queue's error does not keep the others from their turn."""
    outcomes = await asyncio.gather(
        *(turns.take(source, signal) for turns, source in targets), return_exceptions=True
    )
    for (turns, source), outcome in zip(targets, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(
                "host: wake %s -> %s/%s raised %r; the other queues took their turns",
                source.name,
                turns.participant,
                turns.line,
                outcome,
                exc_info=outcome,
            )


async def _nothing() -> None:
    return None


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
