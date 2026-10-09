"""agent-service as four plugins registers exactly what its dataflow wiring declares.

Until both apps start through the host (C7a of the T1 plan), ``app.wiring`` is what the
agent-service process runs; this test holds the two side by side so the switch changes nothing.
It goes when ``app/wiring`` does (C7b).

The wiring side is read from the registries its import fills, after running each wiring module
again (as ``tests/wiring`` does). The plugin side is ``Host.registered()`` plus the same
registries after a start with every IO phase off. Each kind is compared in full, so a
registration missing, added or changed on either side fails: routes with their auth flags, the
clocks with the payload each one builds and the node it hands it to, the inboxes and their
options, the at-start openers, the durable edge and the outbound queues, and the Data classes
the imports register (in fresh interpreters: this test process has imported everything).
"""
from __future__ import annotations

import importlib
import inspect
import json
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.host import Host, MissingService, Registration
from app.messaging.receiving import (
    INBOX_REGISTRY,
    INBOXES_AT_START,
    InboxSpec,
    clear_inboxes,
)
from app.runtime.placement import clear_bindings
from app.runtime.wire import WIRING_REGISTRY, WireSpec, clear_wiring
from app.skills.registry import SkillRegistry

SERVICE_ROOT = Path(__file__).resolve().parents[2]

PLUGIN_MODULES = (
    "app.plugins.ops",
    "app.plugins.operator",
    "app.plugins.skills",
    "app.plugins.living",
)
WIRING_MODULES = (
    "app.wiring.admin",
    "app.wiring.living",
    "app.wiring.messaging",
    "app.wiring.safety",
)


@dataclass(frozen=True)
class Declared:
    wires: list[WireSpec]
    inboxes: dict[str, InboxSpec]
    openers: list


@dataclass(frozen=True)
class Registered:
    registered: tuple[Registration, ...]
    wires: list[WireSpec]
    inboxes: dict[str, InboxSpec]

    def of(self, kind: str) -> list[Registration]:
        return [r for r in self.registered if r.kind == kind]


@dataclass(frozen=True)
class Sides:
    wiring: Declared
    plugins: Registered


def _clear() -> None:
    clear_wiring()
    clear_bindings()
    clear_inboxes()


def _wiring_declarations() -> Declared:
    # The first import of the package runs every submodule once; clear that, then run each
    # submodule again so the registries hold exactly one copy of what the wiring declares.
    importlib.import_module("app.wiring")
    _clear()
    for name in WIRING_MODULES:
        importlib.reload(sys.modules[name])
    declared = Declared(
        wires=list(WIRING_REGISTRY),
        inboxes=dict(INBOX_REGISTRY),
        openers=list(INBOXES_AT_START),
    )
    _clear()
    return declared


def _plugins() -> list:
    return [importlib.import_module(m).PLUGIN for m in PLUGIN_MODULES]


@pytest.fixture
async def sides(monkeypatch, tmp_path) -> Sides:
    wiring = _wiring_declarations()

    # The skills plugin loads SKILLS_DIR in setup; keep the registry other tests see.
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path))
    monkeypatch.setattr(SkillRegistry, "_skills", {})
    host = Host("agent-service", _plugins())
    await host.start(http=None, schema=False, mq=False, clocks=False, tasks=False)
    try:
        plugins = Registered(
            registered=host.registered(),
            wires=list(WIRING_REGISTRY),
            inboxes=dict(INBOX_REGISTRY),
        )
    finally:
        await host.stop()
    return Sides(wiring=wiring, plugins=plugins)


def _sources(wires: list[WireSpec], kind: str):
    for w in wires:
        for source in w.sources:
            if source.kind == kind:
                yield w, source


def test_the_wiring_declares_only_routes_and_clocks_as_sources(sides):
    """Every source the wiring declares is compared below; a new kind would slip through."""
    kinds = {s.kind for w in sides.wiring.wires for s in w.sources}

    assert kinds == {"http", "interval"}


def test_the_plugins_register_the_routes_the_wiring_declares(sides):
    wiring = set()
    for w, source in _sources(sides.wiring.wires, "http"):
        (handler,) = w.consumers
        p = source.params
        # The host only answers synchronously (200 with the handler's answer).
        assert p["response"] is True, p["path"]
        wiring.add(
            (
                p["method"],
                p["path"],
                w.data_type,
                handler,
                p["requires_inner_secret"],
                p["requires_lane_match"],
                p["answers_with_lane"],
            )
        )
    plugins = {
        (
            d["method"],
            d["path"],
            d["request"],
            d["handler"],
            d["inner_secret"],
            d["lane_match"],
            d["answers_with_lane"],
        )
        for d in (r.detail for r in sides.plugins.of("route"))
    }

    assert len(wiring) == 11  # 5 ops routes, 6 operator routes
    assert plugins == wiring


def test_the_plugins_tick_the_clocks_the_wiring_declares(sides):
    """Same name, same seconds, and each tick builds ``data_type(ts=<iso>)`` in the clock loop
    and hands it to the same node, as the dataflow interval source did."""
    ts = datetime(2026, 10, 8, 4, 30, tzinfo=UTC)
    wiring = {}
    for w, source in _sources(sides.wiring.wires, "interval"):
        (node,) = w.consumers
        wiring[w.data_type.__name__] = (
            source.params["seconds"],
            w.data_type(ts=ts.isoformat()),
            node.__wrapped__,
        )
    plugins = {}
    for r in sides.plugins.of("clock"):
        work = r.detail["tick"](ts)
        try:
            assert inspect.iscoroutine(work), r.name
            # The work is the @node wrapper's coroutine, not started: its arguments and the
            # function it wraps are in its frame.
            called = inspect.getcoroutinelocals(work)
            (payload,) = called["args"]
            assert called["kwargs"] == {}
            plugins[r.name] = (r.detail["seconds"], payload, called["fn"])
        finally:
            work.close()

    assert len(wiring) == 5
    assert plugins == wiring


def test_the_plugins_own_the_inboxes_the_wiring_declares(sides):
    options = [f.name for f in fields(InboxSpec) if f.name != "name"]
    wiring = {
        name: {o: getattr(spec, o) for o in options}
        for name, spec in sides.wiring.inboxes.items()
    }
    plugins = {r.name: dict(r.detail) for r in sides.plugins.of("inbox")}

    assert list(wiring) == ["operator"]
    assert plugins == wiring
    # And what the messaging layer will open is the same spec.
    assert sides.plugins.inboxes == sides.wiring.inboxes


def test_the_plugins_open_the_inboxes_the_wiring_opens_at_start(sides):
    plugins = [r.detail["open"] for r in sides.plugins.of("inboxes_at_start")]

    assert len(sides.wiring.openers) == 1
    assert plugins == sides.wiring.openers


def _edges(wires: list[WireSpec]) -> Counter:
    """Wires without a source: the durable edge and the outbound queues, as emit sees them."""
    return Counter(
        (
            w.data_type,
            tuple(w.consumers),
            w.durable,
            tuple((s.kind, tuple(sorted(s.params.items()))) for s in w.sinks),
        )
        for w in wires
        if not w.sources
    )


def test_the_plugins_wire_the_durable_edge_and_the_outbound_queues_the_wiring_does(sides):
    wiring = _edges(sides.wiring.wires)

    assert sorted(w.data_type.__name__ for w in sides.wiring.wires if not w.sources) == [
        "ChatResponseSegment",
        "FilePickedUp",
        "Recall",
    ]
    assert _edges(sides.plugins.wires) == wiring
    # Routes and clocks are the host's now: no dataflow source is left behind.
    assert [w for w in sides.plugins.wires if w.sources] == []
    assert Counter(r.kind for r in sides.plugins.registered if r.kind in ("durable", "outbound")) == {
        "durable": 1,
        "outbound": 2,
    }


def test_each_plugin_registers_its_own_part(sides):
    kinds = Counter((r.plugin, r.kind) for r in sides.plugins.registered)

    assert kinds == {
        ("ops", "route"): 5,
        ("operator", "route"): 6,
        ("operator", "inbox"): 1,
        ("skills", "service"): 1,
        ("skills", "task"): 1,
        ("living", "clock"): 5,
        ("living", "durable"): 1,
        ("living", "outbound"): 2,
        ("living", "inboxes_at_start"): 1,
    }


def _data_registry_after_importing(*modules: str) -> list[str]:
    probe = (
        "import importlib, json\n"
        f"for m in {list(modules)!r}:\n"
        "    importlib.import_module(m)\n"
        "from app.runtime.data import DATA_REGISTRY\n"
        "print(json.dumps(sorted(f'{c.__module__}.{c.__qualname__}' for c in DATA_REGISTRY)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=SERVICE_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_importing_the_plugins_registers_the_data_classes_the_wiring_does():
    """The schema step builds a table per registered Data class (``DATA_REGISTRY``), and a class
    registers when its module is imported: an import the plugins lost is a table not created."""
    wiring = _data_registry_after_importing("app.wiring")
    plugins = _data_registry_after_importing(*PLUGIN_MODULES)

    assert "app.living.pictures.Picture" in wiring
    assert plugins == wiring


def test_living_without_skills_is_refused_naming_both():
    ops, operator, _skills, living = _plugins()

    with pytest.raises(MissingService) as refused:
        Host("agent-service", [ops, operator, living])

    message = str(refused.value)
    assert "'living'" in message
    assert "'skills'" in message
