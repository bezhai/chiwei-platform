"""低耦合的验收：加一个只读来源，就是写一个来源插件、在清单里给它位置（插件、查询工具
``QUERY_TOOLS``、开关能关掉的插件），四个 agent 都用得上它的工具，不改任何 agent 的代码和 prompt；
在启用列表里去掉它，它的工具从四个 agent 里同时消失。

这个模块自己就是那个新来源（潮汐）的插件模块（:data:`PLUGIN`），``world_manifest`` 是 world 的
清单多出那几处。四个 agent 一起跑一遍：主 agent 一轮里报告一个变化、让一个 NPC 出场（感知判断、
NPC 各起一次），再问 world 一个问题（应答起一次）。模型全是替身，只看每一类拿到了哪些工具。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.agent.tooling import tool
from app.host import Plugin
from app.manifests.world import MANIFEST, QUERY_TOOLS, WORLD
from app.messaging.message import Kind, new_message
from app.world import answer, main_agent, npc, perception

from .conftest import ScriptedAgent, ask, sets_wake, tools_built_for, use_tool

FOUR = (
    main_agent.ROUND,
    perception.PERCEPTION,
    npc.NPC,
    answer.ANSWER,
)

REALITY = {
    "check_current_weather",
    "check_hourly_forecast",
    "check_daily_forecast",
    "search_web",
}
RECORDS_AND_TOLD = {"list_records", "read_record", "list_senders", "read_messages_from"}


@tool
async def look_up_tides() -> str:
    """查潮汐：今天几点涨潮、几点落潮。"""
    return "傍晚涨潮。"


PLUGIN = Plugin(name="tides", setup=lambda ctx: ctx.tool(look_up_tides, trim="material"))


def _with_tides(tools: tuple[str, ...]) -> tuple[str, ...]:
    """``QUERY_TOOLS`` 多了潮汐之后，一份以查询工具开头的工具名变成什么样：潮汐排在查询工具最后。"""
    assert tools[: len(QUERY_TOOLS)] == QUERY_TOOLS
    return (*QUERY_TOOLS, "look_up_tides", *tools[len(QUERY_TOOLS) :])


@pytest.fixture
def world_manifest():
    """world 的清单加上潮汐：插件是这个模块，它的工具加进查询工具，开关能关掉它。"""
    with_tides = replace(
        WORLD,
        tools=_with_tides(WORLD.tools),
        switches=replace(WORLD.switches, plugins=(*WORLD.switches.plugins, "tides")),
    )
    return replace(
        MANIFEST,
        plugins=(*MANIFEST.plugins, __name__),
        agents={
            name: replace(spec, tools=_with_tides(spec.tools)) if spec.tools else spec
            for name, spec in MANIFEST.agents.items()
        },
        profiles={**MANIFEST.profiles, "world": with_tides},
    )


def says(text: str) -> ScriptedAgent:
    async def plan(_input):
        return text

    return ScriptedAgent(plan)


def _question():
    return new_message(
        sender="operator", recipient="world", body="厨房什么样？", kind=Kind.QUESTION
    )


async def _run_all_four(world) -> dict[str, list[str]]:
    world.agents[perception.PERCEPTION] = says("没有人会察觉。")
    world.agents[npc.NPC] = says("门卫点了点头。")
    world.agents[answer.ANSWER] = says("厨房里没人。")

    async def a_round():
        await use_tool("report_change", {"change": "起风了。"})
        await use_tool("let_npc_appear", {"npc": "门卫", "situation": "傍晚的校门口。"})
        return await sets_wake()()

    world.runner.plan = a_round
    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )
    await ask(world, _question())
    return {prompt_id: tools_built_for(world, prompt_id) for prompt_id in FOUR}


def _enabled(monkeypatch, names: str) -> None:
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(
        dynamic_config,
        "get",
        lambda k, default="": names if k == WORLD.switches.key else default,
    )


async def test_a_new_source_plugin_placed_in_the_manifest_reaches_all_four_agents(
    world,
):
    tools = await _run_all_four(world)

    assert all(
        tools[agent][: len(QUERY_TOOLS) + 1] == [*QUERY_TOOLS, "look_up_tides"]
        for agent in FOUR
    ), tools


async def test_the_new_source_switched_off_leaves_all_four_agents(world, monkeypatch):
    _enabled(monkeypatch, "records,reality,told")

    tools = await _run_all_four(world)

    for agent in FOUR:
        assert "look_up_tides" not in tools[agent], agent
        assert RECORDS_AND_TOLD | REALITY <= set(tools[agent]), agent


async def test_a_source_left_out_of_the_enabled_list_leaves_all_four_agents(
    world, monkeypatch
):
    _enabled(monkeypatch, "records,told")

    tools = await _run_all_four(world)

    for agent in FOUR:
        assert not {*REALITY, "look_up_tides"} & set(tools[agent]), agent
        assert RECORDS_AND_TOLD <= set(tools[agent]), agent


async def test_the_sub_agents_of_a_round_leave_out_what_the_round_left_out_when_it_started(
    world, monkeypatch
):
    """开关在一轮开始时读一次：一轮里调的感知判断和 NPC 跟主 agent 用同一份，中途改了也不变；
    应答不在哪一轮里，每个问题读一次。"""
    _enabled(monkeypatch, "records,reality,told")
    world.agents[perception.PERCEPTION] = says("没有人会察觉。")
    world.agents[npc.NPC] = says("门卫点了点头。")
    world.agents[answer.ANSWER] = says("厨房里没人。")

    async def a_round():
        _enabled(monkeypatch, "records")
        await use_tool("report_change", {"change": "起风了。"})
        await use_tool("let_npc_appear", {"npc": "门卫", "situation": "傍晚的校门口。"})
        return await sets_wake()()

    world.runner.plan = a_round
    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )
    await ask(world, _question())

    for agent in (perception.PERCEPTION, npc.NPC):
        assert REALITY <= set(tools_built_for(world, agent)), agent
        assert "look_up_tides" not in tools_built_for(world, agent), agent
    assert not REALITY & set(tools_built_for(world, answer.ANSWER))
