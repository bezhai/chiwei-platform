"""低耦合的验收：加一个只读来源，就是写一个来源插件、在清单里给它位置（插件、主 agent 的工具、
开关能关掉的插件），四个 agent 都用得上它的工具，不改任何 agent 的代码和 prompt；在启用列表里
去掉它，它的工具从四个 agent 里同时消失。

这个模块自己就是那个新来源（潮汐）的插件模块（:data:`PLUGIN`），``world_manifest`` 是 world 的
清单多出那几处。四个 agent 一起跑一遍：主 agent 一轮里报告一个变化、让一个 NPC 出场（感知判断、
NPC 各起一次），再问 world 一个问题（应答起一次）。模型全是替身，只看每一类拿到了哪些工具。
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.agent.tooling import tool
from app.manifests.world import MANIFEST, WORLD
from app.messaging.message import Kind, new_message
from app.world import answer, main_agent, npc, perception
from app.world.actions import let_npc_appear, report_change
from app.world.sources import ENABLED_SOURCES_KEY, Source, source_plugin

from .conftest import ScriptedAgent, sets_wake, tools_built_for

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


PLUGIN = source_plugin(Source(name="tides", tools=(look_up_tides,)))


@pytest.fixture
def world_manifest():
    """world 的清单加上潮汐：插件是这个模块，它的工具排在主 agent 的工具最后，开关能关掉它。"""
    with_tides = replace(
        WORLD,
        tools=(*WORLD.tools, "look_up_tides"),
        switches=replace(WORLD.switches, plugins=(*WORLD.switches.plugins, "tides")),
    )
    return replace(
        MANIFEST,
        plugins=(*MANIFEST.plugins, __name__),
        profiles={**MANIFEST.profiles, "world": with_tides},
    )


def says(text: str) -> ScriptedAgent:
    async def plan(_input):
        return text

    return ScriptedAgent(plan)


async def _run_all_four(world) -> dict[str, list[str]]:
    world.agents[perception.PERCEPTION] = says("没有人会察觉。")
    world.agents[npc.NPC] = says("门卫点了点头。")
    world.agents[answer.ANSWER] = says("厨房里没人。")

    async def a_round():
        await report_change.invoke({"change": "起风了。"})
        await let_npc_appear.invoke({"npc": "门卫", "situation": "傍晚的校门口。"})
        return await sets_wake()()

    world.runner.plan = a_round
    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )
    await answer.answer_question(
        new_message(
            sender="operator",
            recipient="world",
            body="厨房什么样？",
            kind=Kind.QUESTION,
        )
    )
    return {prompt_id: tools_built_for(world, prompt_id) for prompt_id in FOUR}


def _enabled(monkeypatch, names: str) -> None:
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(
        dynamic_config,
        "get",
        lambda k, default="": names if k == ENABLED_SOURCES_KEY else default,
    )


async def test_a_new_source_plugin_placed_in_the_manifest_reaches_all_four_agents(
    world,
):
    tools = await _run_all_four(world)

    assert all("look_up_tides" in tools[agent] for agent in FOUR), tools
    assert tools[main_agent.ROUND][-1] == "look_up_tides"


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
