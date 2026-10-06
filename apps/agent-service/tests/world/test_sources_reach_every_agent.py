"""低耦合的验收：加一个只读来源，四个 agent 都用得上它的工具，不改任何 agent 的代码和 prompt；
在启用列表里去掉一个来源，它的工具从四个 agent 里同时消失。

四个 agent 一起跑一遍：主 agent 一轮里报告一个变化、让一个 NPC 出场（感知判断、NPC 各起一次），
再问 world 一个问题（应答起一次）。模型全是替身，只看每一类建 runner 时拿到了哪些工具。
"""
from __future__ import annotations

from app.agent.tooling import tool
from app.messaging.message import Kind, new_message
from app.world import answer, main_agent, npc, perception, sources
from app.world.actions import let_npc_appear, report_change

from .conftest import ScriptedAgent, sets_wake, tools_built_for

FOUR = (
    main_agent.ROUND.prompt_id,
    perception.PERCEPTION.prompt_id,
    npc.NPC.prompt_id,
    answer.ANSWER.prompt_id,
)


@tool
async def look_up_tides() -> str:
    """查潮汐：今天几点涨潮、几点落潮。"""
    return "傍晚涨潮。"


def says(text: str) -> ScriptedAgent:
    async def plan(_input):
        return text

    return ScriptedAgent(plan)


async def _run_all_four(world) -> dict[str, list[str]]:
    world.agents[perception.PERCEPTION.prompt_id] = says("没有人会察觉。")
    world.agents[npc.NPC.prompt_id] = says("门卫点了点头。")
    world.agents[answer.ANSWER.prompt_id] = says("厨房里没人。")

    async def a_round():
        await report_change.invoke({"change": "起风了。"})
        await let_npc_appear.invoke({"npc": "门卫", "situation": "傍晚的校门口。"})
        return await sets_wake()()

    world.runner.plan = a_round
    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )
    await answer.answer_question(
        new_message(sender="operator", recipient="world", body="厨房什么样？", kind=Kind.QUESTION)
    )
    return {prompt_id: tools_built_for(world, prompt_id) for prompt_id in FOUR}


async def test_a_new_read_only_source_reaches_all_four_agents(world):
    sources.register(sources.Source(name="tides", tools=(look_up_tides,)))

    tools = await _run_all_four(world)

    assert all("look_up_tides" in tools[agent] for agent in FOUR), tools


async def test_a_source_left_out_of_the_enabled_list_leaves_all_four_agents(world, monkeypatch):
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(
        dynamic_config,
        "get",
        lambda k, default="": "records,told" if k == sources.ENABLED_SOURCES_KEY else default,
    )

    tools = await _run_all_four(world)

    for agent in FOUR:
        assert not {
            "check_current_weather", "check_hourly_forecast", "check_daily_forecast",
            "search_web",
        } & set(tools[agent])
        assert {"list_records", "read_record", "list_senders", "read_messages_from"} <= set(
            tools[agent]
        )
