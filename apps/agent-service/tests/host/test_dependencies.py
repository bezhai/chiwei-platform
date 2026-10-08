"""Plugin order and the dependency errors.

A plugin names the services it needs (``requires``) and the ones it offers (``provides``). The
host sets plugins up after the plugins whose services they need, keeps the manifest's order
otherwise, and refuses a manifest it cannot run before anything touches the database or the
broker.
"""
from __future__ import annotations

import sys
import types

import pytest

from app.host import (
    DuplicateService,
    Host,
    HostError,
    MissingService,
    ServiceCycle,
    UndeclaredService,
    UnknownApp,
)

from .conftest import no_clocks_no_io, plugin


def _names(host: Host) -> list[str]:
    return [p.name for p in host.plugins]


def test_a_plugin_is_set_up_after_the_plugin_whose_service_it_requires():
    host = Host(
        "agent-service",
        [
            plugin("living", requires=("skills",)),
            plugin("ops"),
            plugin("skills", provides=("skills",)),
        ],
    )

    assert _names(host) == ["ops", "skills", "living"]


def test_plugins_that_do_not_depend_on_each_other_keep_the_manifest_order():
    host = Host("agent-service", [plugin("b"), plugin("a"), plugin("c")])

    assert _names(host) == ["b", "a", "c"]


def test_a_missing_dependency_names_the_plugin_and_the_service():
    """T1 acceptance: starting without a service a plugin needs fails, and says what is missing."""
    with pytest.raises(MissingService) as refused:
        Host(
            "world",
            [plugin("living", requires=("skills",)), plugin("ops", provides=("search",))],
        )

    message = str(refused.value)
    assert "app 'world'" in message
    assert "plugin 'living' requires 'skills'" in message
    assert "'search'" in message, "the error should say what the manifest does provide"


def test_every_missing_dependency_is_in_the_one_error():
    with pytest.raises(MissingService) as refused:
        Host(
            "agent-service",
            [plugin("living", requires=("skills", "phone")), plugin("world", requires=("volume",))],
        )

    message = str(refused.value)
    assert "plugin 'living' requires 'skills'" in message
    assert "plugin 'living' requires 'phone'" in message
    assert "plugin 'world' requires 'volume'" in message


def test_a_dependency_cycle_names_the_plugins_in_it():
    with pytest.raises(ServiceCycle) as refused:
        Host(
            "agent-service",
            [
                plugin("a", requires=("y",), provides=("x",)),
                plugin("b", requires=("x",), provides=("y",)),
                plugin("c", requires=("x",)),
            ],
        )

    message = str(refused.value)
    assert "'a'" in message and "'b'" in message
    assert "'c'" not in message, "c depends on the cycle but is not part of it"


def test_a_plugin_that_requires_its_own_service_is_a_cycle():
    with pytest.raises(ServiceCycle, match="'a'"):
        Host("agent-service", [plugin("a", requires=("x",), provides=("x",))])


def test_two_plugins_providing_the_same_service_is_refused():
    with pytest.raises(DuplicateService) as refused:
        Host("agent-service", [plugin("a", provides=("x",)), plugin("b", provides=("x",))])

    message = str(refused.value)
    assert "'x'" in message and "'a'" in message and "'b'" in message


def test_the_same_plugin_twice_is_refused():
    with pytest.raises(HostError, match="'a'"):
        Host("agent-service", [plugin("a"), plugin("a")])


def test_no_setup_runs_when_the_dependencies_do_not_resolve():
    ran: list[str] = []

    with pytest.raises(MissingService):
        Host(
            "agent-service",
            [
                plugin("ops", lambda ctx: ran.append("ops")),
                plugin("living", lambda ctx: ran.append("living"), requires=("skills",)),
            ],
        )

    assert ran == []


async def test_a_service_reaches_the_plugins_that_require_it():
    skills = object()
    seen: list[object] = []

    host = Host(
        "agent-service",
        [
            plugin("living", lambda ctx: seen.append(ctx.service("skills")), requires=("skills",)),
            plugin("skills", lambda ctx: ctx.provide("skills", skills), provides=("skills",)),
        ],
    )
    await no_clocks_no_io(host)
    try:
        assert seen == [skills]
    finally:
        await host.stop()


async def test_a_plugin_gets_only_the_services_it_requires():
    host = Host(
        "agent-service",
        [
            plugin("skills", lambda ctx: ctx.provide("skills", object()), provides=("skills",)),
            plugin("ops", lambda ctx: ctx.service("skills")),
        ],
    )

    with pytest.raises(UndeclaredService, match="plugin 'ops'.*'skills'"):
        await no_clocks_no_io(host)


@pytest.mark.parametrize(
    "setup, declared, complaint",
    [
        (lambda ctx: ctx.provide("phone", object()), (), "provided 'phone'"),
        (lambda ctx: None, ("phone",), "did not provide 'phone'"),
    ],
    ids=["provided-but-not-declared", "declared-but-not-provided"],
)
async def test_provides_must_match_what_setup_provided(recorded_phases, setup, declared, complaint):
    """Caught right after setup, before the schema step or the broker is touched."""
    host = Host("agent-service", [plugin("living", setup, provides=declared)])

    with pytest.raises(UndeclaredService, match=f"plugin 'living' {complaint}"):
        await host.start(http=None, schema=True, mq=True, clocks=True, tasks=True)

    io = {"ensure_business_schema", "declare_durable_topology", "migrate_schema", "start_consumers"}
    assert io.isdisjoint(recorded_phases.calls)


def test_an_app_the_manifest_does_not_declare_is_refused(monkeypatch):
    import app.deployment as deployment

    monkeypatch.setattr(deployment, "APPS", {"agent-service": (), "world": ()})

    with pytest.raises(UnknownApp) as refused:
        Host.for_app("wrold")

    message = str(refused.value)
    assert "'wrold'" in message
    assert "agent-service" in message and "world" in message


def test_for_app_takes_the_plugin_of_each_manifest_module_in_order(monkeypatch):
    import app.deployment as deployment

    for name in ("first", "second"):
        module = types.ModuleType(f"_host_test_plugins.{name}")
        module.PLUGIN = plugin(name)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(
        deployment,
        "APPS",
        {"agent-service": ("_host_test_plugins.second", "_host_test_plugins.first")},
    )

    host = Host.for_app("agent-service")

    assert host.app_name == "agent-service"
    assert _names(host) == ["second", "first"]


def test_both_apps_are_declared_in_the_manifest():
    from app.deployment import APPS

    assert set(APPS) == {"agent-service", "world"}
