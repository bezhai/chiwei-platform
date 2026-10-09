"""Start and stop: the order of the phases, what stop takes back, restarting in the same process,
cleaning up after a start that failed half-way, and a stop that is cancelled.

The phases that would touch the database, the broker or the clock runner are recorded instead of
run (``recorded_phases``); plugins, registrations and disposers are the real ones.
"""
from __future__ import annotations

import asyncio
from typing import Annotated

import httpx
import pytest
from fastapi import FastAPI

from app.host import Host, HostError
from app.messaging import receiving
from app.runtime import Data, Key, node
from app.runtime.wire import WIRING_REGISTRY
from tests.hosting import start_without_io

from .conftest import plugin


class _Ask(Data):
    q: Annotated[str, Key]

    class Meta:
        transient = True


class _Picked(Data):
    pid: Annotated[str, Key]


class _Said(Data):
    sid: Annotated[str, Key]
    channel: str

    class Meta:
        transient = True


@node
async def _read_it(p: _Picked) -> None:  # pragma: no cover - never delivered here
    raise AssertionError


async def _echo(req: _Ask) -> _Ask:
    return req


async def _ignore(message) -> None:  # pragma: no cover - never delivered here
    raise AssertionError


def _tick(ts):  # pragma: no cover - clocks are recorded, never run, in these tests
    raise AssertionError


def _task(rec, name: str):
    """A task body that notes when the host creates it and when the host cancels it."""

    async def until_cancelled() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            rec.note(f"task.cancelled:{name}")
            raise

    def run():
        rec.note(f"task.start:{name}")
        return until_cancelled()

    return run


def _two_plugins(rec):
    def setup_a(ctx) -> None:
        rec.note("setup:a")
        ctx.route("POST", "/a", _Ask, _echo)
        ctx.task("ta", _task(rec, "ta"))
        ctx.on_stop(lambda: rec.note("on_stop:a"))

    def setup_b(ctx) -> None:
        rec.note("setup:b")
        ctx.clock("tb", 60, _tick)
        ctx.on_stop(lambda: rec.note("on_stop:b"))

    return Host("agent-service", [plugin("a", setup_a), plugin("b", setup_b)])


async def _start_everything(host: Host) -> None:
    await host.start(http=FastAPI(), schema=True, mq=True, clocks=True, tasks=True)


# ---------------------------------------------------------------------------
# phase order
# ---------------------------------------------------------------------------


async def test_start_runs_the_phases_in_order(recorded_phases):
    host = _two_plugins(recorded_phases)

    await _start_everything(host)
    try:
        assert recorded_phases.calls == [
            "lane_provider",
            "setup:a",
            "setup:b",
            "compile_graph",
            "ensure_business_schema",
            "declare_durable_topology",
            "migrate_schema",
            "task.start:ta",
            "start_consumers",
            "start_messaging",
            "bind_route:/a",
            "clocks.start",
        ]
    finally:
        await host.stop()


async def test_stop_runs_the_shutdown_order_and_revokes_last(recorded_phases):
    """Clocks first (rounds they started are cancelled, as today), then the messaging drain, the
    durable consumers, tasks, the broker connection; registrations go last, plugins in reverse."""
    host = _two_plugins(recorded_phases)
    await _start_everything(host)
    recorded_phases.calls.clear()

    await host.stop()

    assert recorded_phases.calls == [
        "clocks.stop",
        "stop_messaging",
        "stop_consumers",
        "task.cancelled:ta",
        "mq.close",
        "on_stop:b",
        "on_stop:a",
        "unbind_route:/a",
    ]


async def test_each_phase_runs_only_when_its_flag_is_set(recorded_phases):
    host = _two_plugins(recorded_phases)

    await start_without_io(host)
    started = list(recorded_phases.calls)
    recorded_phases.calls.clear()
    await host.stop()

    assert started == ["lane_provider", "setup:a", "setup:b", "compile_graph"]
    assert recorded_phases.calls == ["on_stop:b", "on_stop:a"]


async def test_after_start_dynamic_config_reads_the_deployment_lane(monkeypatch):
    """Dynamic Config resolves per lane: a coe process must read its lane's config, not prod's.
    Ported from the dataflow startup's test, which set the same provider."""
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(dynamic_config, "_lane_provider", None)
    host = Host("agent-service", [plugin("a")])
    await start_without_io(host)
    try:
        monkeypatch.setenv("LANE", "coe-feedwl")
        assert dynamic_config._get_lane() == "coe-feedwl"
        monkeypatch.delenv("LANE", raising=False)
        assert dynamic_config._get_lane() == "prod"
    finally:
        await host.stop()


async def test_starting_twice_without_stopping_is_refused(recorded_phases):
    host = _two_plugins(recorded_phases)
    await start_without_io(host)
    try:
        with pytest.raises(HostError, match="already started"):
            await start_without_io(host)
    finally:
        await host.stop()


async def test_stopping_a_host_that_never_started_does_nothing(recorded_phases):
    host = _two_plugins(recorded_phases)

    await host.stop()

    assert recorded_phases.calls == []


# ---------------------------------------------------------------------------
# a start that fails half-way cleans up what it started
# ---------------------------------------------------------------------------


async def test_a_failed_start_stops_what_already_started_then_re_raises(recorded_phases):
    host = _two_plugins(recorded_phases)
    recorded_phases.fail_at = "start_messaging"

    with pytest.raises(RuntimeError, match="start_messaging failed"):
        await _start_everything(host)

    after = recorded_phases.calls[recorded_phases.calls.index("start_messaging") + 1 :]
    assert after == [
        "stop_messaging",
        "stop_consumers",
        "task.cancelled:ta",
        "mq.close",
        "on_stop:b",
        "on_stop:a",
    ], "clocks and routes never started, so only the rest is stopped"
    assert host.registered() == ()


async def test_a_failing_setup_touches_nothing_and_revokes_the_plugins_before_it(recorded_phases):
    def setup_a(ctx) -> None:
        recorded_phases.note("setup:a")
        ctx.inbox("operator", on_message=_ignore)
        ctx.on_stop(lambda: recorded_phases.note("on_stop:a"))

    def setup_b(ctx) -> None:
        ctx.route("GET", "/b", _Ask, _echo)
        raise RuntimeError("setup b failed")

    host = Host("agent-service", [plugin("a", setup_a), plugin("b", setup_b)])

    with pytest.raises(RuntimeError, match="setup b failed"):
        await _start_everything(host)

    assert recorded_phases.calls == ["lane_provider", "setup:a", "on_stop:a"]
    assert host.registered() == ()
    assert receiving.INBOX_REGISTRY == {}


async def test_a_failure_before_the_broker_is_touched_does_not_stop_the_broker(recorded_phases):
    host = _two_plugins(recorded_phases)
    recorded_phases.fail_at = "ensure_business_schema"

    with pytest.raises(RuntimeError, match="ensure_business_schema failed"):
        await _start_everything(host)

    after = recorded_phases.calls[recorded_phases.calls.index("ensure_business_schema") + 1 :]
    assert after == ["on_stop:b", "on_stop:a"]


async def test_a_failing_clock_start_still_stops_every_other_phase(recorded_phases):
    host = _two_plugins(recorded_phases)
    recorded_phases.fail_at = "clocks.start"

    with pytest.raises(RuntimeError, match="clocks.start failed"):
        await _start_everything(host)

    after = recorded_phases.calls[recorded_phases.calls.index("clocks.start") + 1 :]
    assert after == [
        "clocks.stop",
        "stop_messaging",
        "stop_consumers",
        "task.cancelled:ta",
        "mq.close",
        "on_stop:b",
        "on_stop:a",
        "unbind_route:/a",
    ]


async def test_a_failing_stop_phase_does_not_skip_the_rest(recorded_phases):
    host = _two_plugins(recorded_phases)
    await _start_everything(host)
    recorded_phases.calls.clear()
    recorded_phases.fail_at = "stop_messaging"

    with pytest.raises(RuntimeError, match="stop_messaging failed"):
        await host.stop()

    assert recorded_phases.calls == [
        "clocks.stop",
        "stop_messaging",
        "stop_consumers",
        "task.cancelled:ta",
        "mq.close",
        "on_stop:b",
        "on_stop:a",
        "unbind_route:/a",
    ]
    assert host.registered() == ()


# ---------------------------------------------------------------------------
# a stop that is cancelled, and a CancelledError nothing cancelled the stop for
# ---------------------------------------------------------------------------


def _held(entered: asyncio.Event):
    """A cleanup callback that says it started, then waits until it is cancelled."""

    async def hold() -> None:
        entered.set()
        await asyncio.Event().wait()

    return hold


async def _raise_cancelled() -> None:
    """A cleanup callback that raises CancelledError although nothing cancelled anything."""
    raise asyncio.CancelledError


async def test_a_stop_cancelled_in_an_on_stop_leaves_the_rest_to_the_next_stop():
    """The reviewer's first repro: before the fix the second stop returned at once, the route and
    the inbox stayed, and the restart failed with a duplicate registration."""
    entered = asyncio.Event()
    ran: list[str] = []
    setups: list[int] = []
    app = FastAPI()

    def setup(ctx) -> None:
        setups.append(1)
        ctx.route("POST", "/kept", _Ask, _echo)
        ctx.inbox("kept", on_message=_ignore)
        ctx.on_stop(lambda: ran.append("on_stop:before"))
        if len(setups) == 1:
            ctx.on_stop(_held(entered))

    host = Host("agent-service", [plugin("p", setup)])
    await host.start(http=app, schema=False, mq=False, clocks=False, tasks=False)
    stopping = asyncio.create_task(host.stop())
    await entered.wait()
    stopping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert "/kept" in _paths(app)
    assert set(receiving.INBOX_REGISTRY) == {"kept"}

    async with asyncio.timeout(5):  # the interrupted on_stop is not run again
        await host.stop()

    assert host.registered() == ()
    assert "/kept" not in _paths(app)
    assert receiving.INBOX_REGISTRY == {}
    assert ran == ["on_stop:before"]

    await host.start(http=app, schema=False, mq=False, clocks=False, tasks=False)
    try:
        assert _paths(app).count("/kept") == 1
        assert set(receiving.INBOX_REGISTRY) == {"kept"}
    finally:
        await host.stop()


async def test_a_stop_cancelled_in_a_phase_leaves_the_later_phases_to_the_next_stop(
    recorded_phases, monkeypatch
):
    """Each piece of cleanup runs at most once: the drain that was cancelled (perhaps because it
    hung) is not run again; everything after it is, by the next stop."""
    import app.host.host as host_module

    host = _two_plugins(recorded_phases)
    await _start_everything(host)
    recorded_phases.calls.clear()
    entered = asyncio.Event()

    async def drain_that_hangs() -> None:
        recorded_phases.note("stop_messaging")
        await _held(entered)()

    monkeypatch.setattr(host_module, "stop_messaging", drain_that_hangs)
    stopping = asyncio.create_task(host.stop())
    await entered.wait()
    stopping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopping
    assert recorded_phases.calls == ["clocks.stop", "stop_messaging"]

    async with asyncio.timeout(5):
        await host.stop()

    assert recorded_phases.calls == [
        "clocks.stop",
        "stop_messaging",
        "stop_consumers",
        "task.cancelled:ta",
        "mq.close",
        "on_stop:b",
        "on_stop:a",
        "unbind_route:/a",
    ]
    assert host.registered() == ()


async def test_a_cancelled_stop_is_raised_even_when_the_step_it_hit_catches_it():
    """Waiting for a cancelled task to end catches CancelledError (the host's tasks phase and the
    clock runner both do). A cancellation of the stop that lands there must still come out."""
    ending = asyncio.Event()

    async def slow_to_end() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            ending.set()
            await asyncio.sleep(0.05)
            raise

    def setup(ctx) -> None:
        ctx.task("slow", slow_to_end)
        ctx.inbox("kept", on_message=_ignore)

    host = Host("agent-service", [plugin("p", setup)])
    await start_without_io(host, tasks=True)
    await asyncio.sleep(0)
    stopping = asyncio.create_task(host.stop())
    await ending.wait()
    stopping.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopping

    await host.stop()
    assert host.registered() == ()
    assert receiving.INBOX_REGISTRY == {}


async def test_a_cancelled_error_a_callback_raises_is_a_failure_not_a_cancellation():
    ran: list[str] = []

    def setup(ctx) -> None:
        ctx.inbox("kept", on_message=_ignore)
        ctx.on_stop(lambda: ran.append("on_stop:before"))
        ctx.on_stop(_raise_cancelled)

    host = Host("agent-service", [plugin("p", setup)])
    await start_without_io(host)

    with pytest.raises(HostError, match="CancelledError") as caught:
        await host.stop()

    assert isinstance(caught.value.__cause__, asyncio.CancelledError)
    assert ran == ["on_stop:before"]
    assert host.registered() == ()
    assert receiving.INBOX_REGISTRY == {}


async def test_a_cancelled_error_from_the_cleanup_does_not_replace_the_start_error():
    """The reviewer's second repro: a cleanup callback's CancelledError used to come out of start
    in place of the setup's RuntimeError, and the cleanup stopped there."""

    def setup_a(ctx) -> None:
        ctx.inbox("operator", on_message=_ignore)
        ctx.on_stop(_raise_cancelled)

    def setup_b(ctx) -> None:
        raise RuntimeError("setup b failed")

    host = Host("agent-service", [plugin("a", setup_a), plugin("b", setup_b)])

    with pytest.raises(RuntimeError, match="setup b failed"):
        await start_without_io(host)

    assert host.registered() == ()
    assert receiving.INBOX_REGISTRY == {}


async def test_a_start_that_is_cancelled_still_cleans_up_everything(recorded_phases, monkeypatch):
    """The cancellation that stopped the start was delivered before the cleanup began; only a
    cancellation that comes during the cleanup interrupts it."""
    import app.host.host as host_module

    entered = asyncio.Event()

    async def schema_that_hangs() -> None:
        recorded_phases.note("ensure_business_schema")
        await _held(entered)()

    monkeypatch.setattr(host_module, "ensure_business_schema", schema_that_hangs)
    host = _two_plugins(recorded_phases)
    starting = asyncio.create_task(_start_everything(host))
    await entered.wait()
    starting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await starting

    after = recorded_phases.calls[recorded_phases.calls.index("ensure_business_schema") + 1 :]
    assert after == ["on_stop:b", "on_stop:a"]
    assert host.registered() == ()


async def test_a_start_cancelled_while_it_cleans_up_raises_the_cancellation_with_its_error():
    """Cancelling the task while a failed start cleans up raises the cancellation, the start's
    error kept as its context; the next stop finishes the cleanup."""
    entered = asyncio.Event()

    def setup_a(ctx) -> None:
        ctx.inbox("operator", on_message=_ignore)
        ctx.on_stop(_held(entered))

    def setup_b(ctx) -> None:
        raise RuntimeError("setup b failed")

    host = Host("agent-service", [plugin("a", setup_a), plugin("b", setup_b)])
    starting = asyncio.create_task(start_without_io(host))
    await entered.wait()
    starting.cancel()

    with pytest.raises(asyncio.CancelledError) as caught:
        await starting
    assert isinstance(caught.value.__context__, RuntimeError)
    assert set(receiving.INBOX_REGISTRY) == {"operator"}

    async with asyncio.timeout(5):
        await host.stop()

    assert host.registered() == ()
    assert receiving.INBOX_REGISTRY == {}


# ---------------------------------------------------------------------------
# what stop takes back
# ---------------------------------------------------------------------------


def _everything_plugin(ran: list[str], *, inbox_name: str = "probe"):
    async def open_them() -> None:  # pragma: no cover - mq is off in these tests
        raise AssertionError

    def setup(ctx) -> None:
        ran.append("setup")
        ctx.route("POST", "/everything", _Ask, _echo)
        ctx.inbox(inbox_name, on_message=_ignore)
        ctx.inboxes_at_start(open_them)
        ctx.durable(_Picked, _read_it)
        ctx.outbound(_Said, "recall")
        ctx.provide("thing", object())
        ctx.clock("c", 60, _tick)
        ctx.task("t", lambda: asyncio.Event().wait())
        ctx.on_stop(lambda: ran.append("on_stop"))

    return plugin("everything", setup, provides=("thing",))


def _paths(app: FastAPI) -> list[str]:
    return [getattr(r, "path", None) for r in app.router.routes]


async def test_stop_revokes_every_registration():
    """T1 acceptance (the registry half): after stop nothing a plugin registered is left behind."""
    ran: list[str] = []
    app = FastAPI()
    host = Host("agent-service", [_everything_plugin(ran)])
    await host.start(http=app, schema=False, mq=False, clocks=False, tasks=True)

    assert {(r.kind, r.name) for r in host.registered()} == {
        ("route", "POST /everything"),
        ("inbox", "probe"),
        ("inboxes_at_start", "_everything_plugin.<locals>.open_them"),
        ("durable", "_Picked->_read_it"),
        ("outbound", "_Said->recall"),
        ("service", "thing"),
        ("clock", "c"),
        ("task", "t"),
        ("on_stop", "_everything_plugin.<locals>.setup.<locals>.<lambda>"),
    }
    assert "/everything" in _paths(app)
    assert set(receiving.INBOX_REGISTRY) == {"probe"}
    assert len(receiving.INBOXES_AT_START) == 1
    assert [w.data_type for w in WIRING_REGISTRY] == [_Picked, _Said]
    app.openapi()

    await host.stop()

    assert host.registered() == ()
    assert "/everything" not in _paths(app)
    assert app.openapi_schema is None, "a cached OpenAPI document would still list the route"
    assert "/everything" not in app.openapi()["paths"]
    assert receiving.INBOX_REGISTRY == {}
    assert receiving.INBOXES_AT_START == []
    assert WIRING_REGISTRY == []
    assert ran == ["setup", "on_stop"]


async def test_the_durable_and_outbound_wires_carry_emits_only_while_registered(monkeypatch):
    import app.runtime.durable as durable_mod
    import app.runtime.sink_dispatch as sink_mod
    from app.runtime.emit import emit
    published: list[tuple[str, object]] = []

    async def publish_durable(w, consumer, data) -> None:
        published.append(("durable", consumer))

    async def dispatch_mq_sink(spec, data) -> None:
        published.append(("outbound", spec.params["queue"]))

    monkeypatch.setattr(durable_mod, "publish_durable", publish_durable)
    monkeypatch.setattr(sink_mod, "_dispatch_mq_sink", dispatch_mq_sink)
    host = Host("agent-service", [_everything_plugin([])])
    await start_without_io(host)

    await emit(_Picked(pid="1"))
    await emit(_Said(sid="1", channel="lark"))
    await host.stop()
    await emit(_Picked(pid="2"))
    await emit(_Said(sid="2", channel="lark"))

    assert published == [("durable", _read_it), ("outbound", "recall")]


async def test_a_disposer_takes_back_its_registration_while_the_host_runs():
    disposers: dict[str, object] = {}
    app = FastAPI()

    def setup(ctx) -> None:
        disposers["route"] = ctx.route("POST", "/gone", _Ask, _echo)
        ctx.route("POST", "/kept", _Ask, _echo)
        disposers["inbox"] = ctx.inbox("gone", on_message=_ignore)
        ctx.inbox("kept", on_message=_ignore)

    host = Host("agent-service", [plugin("p", setup)])
    await host.start(http=app, schema=False, mq=False, clocks=False, tasks=False)
    try:
        await disposers["route"]()
        await disposers["inbox"]()
        await disposers["route"]()  # a second call does nothing

        assert "/gone" not in _paths(app) and "/kept" in _paths(app)
        assert set(receiving.INBOX_REGISTRY) == {"kept"}
        assert {r.name for r in host.registered()} == {"POST /kept", "kept"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://host"
        ) as client:
            assert (await client.post("/gone", json={"q": "x"})).status_code == 404
            assert (await client.post("/kept", json={"q": "x"})).status_code == 200
    finally:
        await host.stop()


async def test_registering_outside_setup_is_refused():
    kept: list = []
    host = Host("agent-service", [plugin("p", kept.append)])
    await start_without_io(host)
    try:
        with pytest.raises(HostError, match="only during setup"):
            kept[0].clock("late", 60, _tick)
    finally:
        await host.stop()


async def test_two_registrations_with_one_name_are_refused():
    def setup(ctx) -> None:
        ctx.clock("c", 60, _tick)
        ctx.clock("c", 30, _tick)

    host = Host("agent-service", [plugin("p", setup)])

    with pytest.raises(HostError, match="clock 'c'"):
        await start_without_io(host)
    assert host.registered() == ()


async def test_registered_shows_what_each_registration_declared():
    async def on_question(message) -> str | None:  # pragma: no cover
        return None

    def setup(ctx) -> None:
        ctx.clock("c", 60, _tick)
        ctx.route("GET", "/r", _Ask, _echo, inner_secret=True, lane_match=True)
        ctx.inbox("probe", on_message=_ignore, on_question=on_question)
        ctx.outbound(_Said, "recall")

    host = Host("world", [plugin("p", setup)])
    await start_without_io(host)
    try:
        by_kind = {r.kind: r for r in host.registered()}
    finally:
        await host.stop()

    assert all(r.plugin == "p" for r in by_kind.values())
    assert by_kind["clock"].detail["seconds"] == 60.0
    assert isinstance(by_kind["clock"].detail["seconds"], float)
    assert by_kind["route"].detail == {
        "method": "GET",
        "path": "/r",
        "request": _Ask,
        "handler": _echo,
        "inner_secret": True,
        "lane_match": True,
        "answers_with_lane": False,
    }
    assert by_kind["inbox"].detail["on_message"] is _ignore
    assert by_kind["inbox"].detail["on_question"] is on_question
    assert by_kind["outbound"].detail == {"data_type": _Said, "queue": "recall"}


# ---------------------------------------------------------------------------
# restarting in the same process
# ---------------------------------------------------------------------------


async def test_the_host_restarts_in_the_same_process(monkeypatch):
    """Stop takes everything back, so a second start sets every plugin up again from scratch:
    no "inbox already declared", no route bound twice, the clock ticks again."""
    monkeypatch.delenv("LANE", raising=False)
    ticks: list[object] = []
    setups: list[int] = []
    app = FastAPI()

    def setup(ctx) -> None:
        setups.append(1)
        ctx.route("POST", "/again", _Ask, _echo)
        ctx.inbox("again", on_message=_ignore)

        async def note(ts) -> None:
            ticks.append(ts)

        ctx.clock("again", 0.05, note)

    host = Host("agent-service", [plugin("p", setup)])
    for _ in range(2):
        before = len(ticks)
        await host.start(http=app, schema=False, mq=False, clocks=True, tasks=False)
        await asyncio.sleep(0.2)
        assert _paths(app).count("/again") == 1
        assert set(receiving.INBOX_REGISTRY) == {"again"}
        await host.stop()
        assert len(ticks) > before

    assert setups == [1, 1]
    assert "/again" not in _paths(app)


# ---------------------------------------------------------------------------
# inboxes whose names are known only at start
# ---------------------------------------------------------------------------


async def test_the_host_knows_which_inboxes_each_opener_declared(recorded_phases, monkeypatch):
    """The opener runs inside messaging's start; the host watches the inbox registry around it,
    so taking the opener back also takes back the inboxes it opened, and only those."""
    import app.host.host as host_module

    monkeypatch.setattr(
        host_module, "start_messaging", receiving._open_inboxes_named_at_start
    )
    disposers: list = []

    async def open_sisters() -> None:
        for name in ("赤尾", "绫奈"):
            receiving.inbox(name, on_message=_ignore)

    def sisters(ctx) -> None:
        disposers.append(ctx.inboxes_at_start(open_sisters))

    def operator(ctx) -> None:
        ctx.inbox("operator", on_message=_ignore)

    host = Host("agent-service", [plugin("operator", operator), plugin("sisters", sisters)])
    await start_without_io(host, mq=True)
    try:
        opener = next(r for r in host.registered() if r.kind == "inboxes_at_start")
        assert opener.plugin == "sisters"
        assert opener.detail["declared"] == ("赤尾", "绫奈")

        await disposers[0]()

        assert set(receiving.INBOX_REGISTRY) == {"operator"}
    finally:
        await host.stop()
    assert receiving.INBOX_REGISTRY == {}


async def test_a_failing_opener_leaves_no_inbox_behind(recorded_phases, monkeypatch):
    import app.host.host as host_module

    monkeypatch.setattr(
        host_module, "start_messaging", receiving._open_inboxes_named_at_start
    )

    async def half_named() -> None:
        receiving.inbox("赤尾", on_message=_ignore)
        raise RuntimeError("两个人的显示名一样")

    def setup(ctx) -> None:
        ctx.inbox("operator", on_message=_ignore)
        ctx.inboxes_at_start(half_named)

    host = Host("agent-service", [plugin("p", setup)])

    with pytest.raises(RuntimeError, match="两个人的显示名一样"):
        await start_without_io(host, mq=True)

    assert receiving.INBOX_REGISTRY == {}
    assert receiving.INBOXES_AT_START == []
    assert host.registered() == ()
