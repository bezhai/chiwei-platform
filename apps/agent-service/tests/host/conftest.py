"""Fixtures shared by the plugin host tests.

Most tests run the host without a broker or a database: the phases that would touch them are
switched off by ``start``'s flags or replaced by recorders (:func:`recorded_phases`). The tests
that prove consumers really stop run on the messaging suite's broker (RabbitMQ with the
delayed-message plugin), imported here.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from app.host import Plugin
from tests.messaging.conftest import broker, delayed_broker  # noqa: F401


async def no_clocks_no_io(host, **flags) -> None:
    """Start ``host`` with every IO phase off unless a flag turns it on."""
    options = {"http": None, "schema": False, "mq": False, "clocks": False, "tasks": False}
    options.update(flags)
    await host.start(**options)


def plugin(name: str, setup: Callable | None = None, **kwargs) -> Plugin:
    return Plugin(name=name, setup=setup or (lambda ctx: None), **kwargs)


class Recorder:
    """Stands in for every function a start or stop phase delegates to, and writes down the order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_at: str | None = None

    def note(self, name: str) -> None:
        self.calls.append(name)
        if name == self.fail_at:
            raise RuntimeError(f"{name} failed")

    def sync(self, name: str):
        def call(*_args, **_kwargs):
            self.note(name)

        return call

    def coro(self, name: str):
        async def call(*_args, **_kwargs):
            self.note(name)
            # The real calls wait on the network; yielding here lets tasks the host already
            # created start running, as they would.
            await asyncio.sleep(0)

        return call


@pytest.fixture
def recorded_phases(monkeypatch) -> Recorder:
    """Replace what each phase delegates to with a recorder.

    Plugins, registrations, disposers and tasks stay real; only the calls into dataflow,
    messaging, the broker, the schema and the clock runner are written down instead of made.
    """
    import app.host.host as host_module

    rec = Recorder()

    class _DynamicConfig:
        def set_lane_provider(self, _provider) -> None:
            rec.note("lane_provider")

    class _Mq:
        async def close(self) -> None:
            rec.note("mq.close")

    class _Clocks:
        def __init__(self, _owner: str) -> None:
            pass

        async def start(self, clocks, *, enabled: bool) -> None:
            rec.note("clocks.start")

        async def remove(self, name: str) -> None:
            rec.note(f"clocks.remove:{name}")

        async def stop(self) -> None:
            rec.note("clocks.stop")

    def _bind_route(_app, spec):
        rec.note(f"bind_route:{spec.path}")
        return spec.path

    def _unbind_route(_app, route) -> None:
        rec.note(f"unbind_route:{route}")

    monkeypatch.setattr(host_module, "dynamic_config", _DynamicConfig())
    monkeypatch.setattr(host_module, "compile_graph", rec.sync("compile_graph"))
    monkeypatch.setattr(host_module, "ensure_business_schema", rec.coro("ensure_business_schema"))
    monkeypatch.setattr(
        host_module, "declare_durable_topology", rec.coro("declare_durable_topology")
    )
    monkeypatch.setattr(host_module, "migrate_schema", rec.coro("migrate_schema"))
    monkeypatch.setattr(host_module, "start_consumers", rec.coro("start_consumers"))
    monkeypatch.setattr(host_module, "start_messaging", rec.coro("start_messaging"))
    monkeypatch.setattr(host_module, "stop_messaging", rec.coro("stop_messaging"))
    monkeypatch.setattr(host_module, "stop_consumers", rec.coro("stop_consumers"))
    monkeypatch.setattr(host_module, "mq", _Mq())
    monkeypatch.setattr(host_module, "Clocks", _Clocks)
    monkeypatch.setattr(host_module, "bind_route", _bind_route)
    monkeypatch.setattr(host_module, "unbind_route", _unbind_route)
    return rec
