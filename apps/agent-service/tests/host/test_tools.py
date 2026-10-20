"""A plugin declares each of its tools to the host (``ctx.tool``): how trimming treats what the tool
returns, and whether calling it ends the round. The declaration is a ``ToolSpec``; the tool's
schema is the one reflected from the function, unchanged.
"""
from __future__ import annotations

import pytest

from app.agent.contracts import ToolSpec
from app.agent.tooling import tool
from app.host import Host, HostError
from tests.hosting import start_without_io

from .conftest import plugin


@tool
async def look_around() -> str:
    """看一眼周围。"""
    return "客厅里没人。"


@tool
async def stop_for_now() -> str:
    """这一轮到此为止。"""
    return "就到这儿。"


async def test_a_declared_tool_shows_its_trim_class_and_whether_it_ends_the_round():
    def setup(ctx) -> None:
        ctx.tool(look_around, trim="material")
        ctx.tool(stop_for_now, trim="kept", ends_round=True)

    host = Host("agent-service", [plugin("life", setup)])
    await start_without_io(host)
    try:
        tools = [r for r in host.registered() if r.kind == "tool"]
    finally:
        await host.stop()

    assert [(r.plugin, r.name) for r in tools] == [
        ("life", "look_around"),
        ("life", "stop_for_now"),
    ]
    assert tools[0].detail == {"spec": ToolSpec(look_around, trim="material")}
    assert tools[1].detail == {"spec": ToolSpec(stop_for_now, trim="kept", ends_round=True)}


async def test_two_plugins_declaring_one_tool_name_are_refused():
    host = Host(
        "agent-service",
        [
            plugin("life", lambda ctx: ctx.tool(look_around, trim="material")),
            plugin("web", lambda ctx: ctx.tool(look_around, trim="kept")),
        ],
    )

    with pytest.raises(HostError, match="tool 'look_around'"):
        await start_without_io(host)


async def test_a_tool_without_a_trim_class_is_refused():
    host = Host(
        "agent-service",
        [plugin("life", lambda ctx: ctx.tool(look_around, trim="sometimes"))],
    )

    with pytest.raises(ValueError, match="trim"):
        await start_without_io(host)


async def test_a_disposer_takes_the_declaration_back():
    disposers = []
    host = Host(
        "agent-service",
        [plugin("life", lambda ctx: disposers.append(ctx.tool(look_around, trim="material")))],
    )
    await start_without_io(host)
    try:
        await disposers[0]()
        assert [r for r in host.registered() if r.kind == "tool"] == []
    finally:
        await host.stop()
