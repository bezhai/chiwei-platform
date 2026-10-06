"""NPC 出场：主 agent 只说让谁出场、在什么情境下；NPC 的言行出自扮演他的那次调用，原样交给
感知判断，发给居民的话出自 NPC 扮演，不出自主 agent。

模型换成替身（主 agent 在一轮的 context 里真调 ``let_npc_appear``），通信机制的 ``send`` 换成替身。
"""
from __future__ import annotations

from app.messaging.message import Kind, new_message
from app.world import npc, perception
from app.world.actions import let_npc_appear
from app.world.agents import when
from app.world.sources import query_tools

from .conftest import LANE, ScriptedAgent, sets_wake, tools_built_for

SITUATION = "下午四点，学校的美术教室，一个学生把一幅画递给美术老师。"
LINES = "美术老师接过画，举到窗边看了一会儿，说：「这里的光很好，影子可以再深一点。」"


def plays(said: str):
    async def plan(_input):
        return said

    return ScriptedAgent(plan)


def passes_on_what_it_was_told(to: str):
    """感知判断替身：判断 ``to`` 会察觉到，把它收到的那个变化原样作为他察觉到的。"""

    async def plan(perception_input):
        change = perception_input.split("【世界里发生的变化】\n", 1)[1]
        await perception.someone_notices.invoke({"who": to, "what": change, "right_away": True})
        return "判断完了。"

    return ScriptedAgent(plan)


def lets_appear(*appearances: tuple[str, str]):
    results: list = []

    async def plan():
        for who, situation in appearances:
            results.append(
                await let_npc_appear.invoke({"npc": who, "situation": situation})
            )
        return await sets_wake()()

    plan.results = results
    return plan


TRIGGER = new_message(
    sender="ayana", recipient="world", body="我把画拿给老师看。", kind=Kind.MESSAGE
)


async def _a_round(world, plan):
    world.runner.plan = plan
    await world.deliver(TRIGGER)
    return plan.results


async def test_what_the_resident_is_told_comes_from_the_npc_agent_not_the_main_agent(world):
    player = plays(LINES)
    judge = passes_on_what_it_was_told("ayana")
    world.agents[npc.NPC.prompt_id] = player
    world.agents[perception.PERCEPTION.prompt_id] = judge

    [result] = await _a_round(world, lets_appear(("美术老师", SITUATION)))

    # 扮演他的那次调用拿到的是主 agent 给的人和情境。
    assert "美术老师" in player.inputs[0] and SITUATION in player.inputs[0]
    # 交给感知判断的变化就是 NPC 的输出，原样，没有主 agent 写的情境。
    assert judge.inputs[0].split("【世界里发生的变化】\n", 1)[1] == LINES
    assert SITUATION not in judge.inputs[0]
    # 发给居民的话出自 NPC 扮演。
    assert world.sent == [{"sender": "world", "recipient": "ayana", "body": LINES}]
    # 主 agent 看到他的言行和告知了谁。
    assert LINES in result and "ayana" in result


async def test_perception_of_an_npcs_words_sees_the_message_that_woke_this_round(world):
    """跟报告变化一样：感知判断知道这一轮是谁发来的什么叫醒的，NPC 的言行接在后面。"""
    judge = passes_on_what_it_was_told("ayana")
    world.agents[npc.NPC.prompt_id] = plays(LINES)
    world.agents[perception.PERCEPTION.prompt_id] = judge

    await _a_round(world, lets_appear(("美术老师", SITUATION)))

    [seen] = judge.inputs
    assert seen.split("\n")[1:] == [
        "【叫醒世界的消息】这一轮世界收到 1 条消息，按到达的先后：",
        f"（1）ayana 发来（{when(TRIGGER.time)}）：",
        "我把画拿给老师看。",
        "【世界里发生的变化】",
        LINES,
    ]


async def test_the_npc_agent_has_its_own_prompt_trace_cost_and_only_the_sources_tools(world):
    world.agents[npc.NPC.prompt_id] = plays(LINES)
    world.agents[perception.PERCEPTION.prompt_id] = passes_on_what_it_was_told("ayana")

    await _a_round(world, lets_appear(("美术老师", SITUATION)))

    [config] = [c for c, _ in world.built if c.prompt_id == "world_npc"]
    assert config.trace_name == "world-npc"
    assert tools_built_for(world, "world_npc") == [t.name for t in await query_tools()]
    assert len([c for c in world.costs if c["round_id"].startswith("world-npc:")]) == 1


async def test_each_appearance_is_its_own_npc_call_and_its_own_perception_call(world):
    player = plays(LINES)
    judge = passes_on_what_it_was_told("ayana")
    world.agents[npc.NPC.prompt_id] = player
    world.agents[perception.PERCEPTION.prompt_id] = judge

    await _a_round(world, lets_appear(("美术老师", SITUATION), ("门卫", "放学时的校门口。")))

    assert len(player.inputs) == 2 and "门卫" in player.inputs[1]
    assert len(judge.inputs) == 2


async def test_an_npc_who_says_and_does_nothing_tells_nobody(world):
    judge = passes_on_what_it_was_told("ayana")
    world.agents[npc.NPC.prompt_id] = plays("   ")
    world.agents[perception.PERCEPTION.prompt_id] = judge

    [result] = await _a_round(world, lets_appear(("美术老师", SITUATION)))

    assert judge.inputs == [] and world.sent == []
    assert "没有告知" in result


async def test_the_main_agent_sees_the_npcs_own_words_even_if_nobody_notices_them(world):
    async def nobody(_input):
        return "没有人会察觉。"

    world.agents[npc.NPC.prompt_id] = plays(LINES)
    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(nobody)

    [result] = await _a_round(world, lets_appear(("美术老师", SITUATION)))

    assert LINES in result and "没有人会察觉到" in result
    assert world.sent == []


async def test_npc_words_messaging_could_not_carry_are_not_kept_or_judged(world, volume):
    """NPC 的言行要原样记进 unfinished、交给感知判断；存不下的字当作这次扮演没做成，交回主 agent。"""
    judge = passes_on_what_it_was_told("ayana")
    world.agents[npc.NPC.prompt_id] = plays("门卫说：「今天\x00关门早。」")
    world.agents[perception.PERCEPTION.prompt_id] = judge

    [result] = await _a_round(world, lets_appear(("门卫", "放学时的校门口。")))

    assert "没有出场" in result
    assert judge.inputs == [] and world.sent == []
    assert not (volume / LANE / "unfinished.json").exists()


async def test_a_situation_messaging_could_not_carry_is_handed_back_before_anyone_plays(world):
    player = plays(LINES)
    world.agents[npc.NPC.prompt_id] = player

    [result] = await _a_round(world, lets_appear(("美术老师", "美术教室\x00里。")))

    assert "没有出场" in result
    assert player.inputs == []
