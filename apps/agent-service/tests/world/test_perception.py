"""感知判断：主 agent 报告一个变化 → 一次感知判断 → 每个判断会察觉的人一条告知；以及告知居民
只有这一条路。

模型换成替身（主 agent 在一轮的 context 里真调 ``report_change``，感知判断的替身在它那次调用的
context 里真调 ``someone_notices``），通信机制的 ``send`` 换成替身。
"""
from __future__ import annotations

import ast
from pathlib import Path

import app.world
from app.messaging.message import Kind, new_message
from app.world import main_agent, perception
from app.world.actions import ACTIONS, report_change
from app.world.sources import query_tools

from .conftest import ScriptedAgent, sets_wake, tools_built_for


def judges(*judgments: tuple[str, str], said: str = "判断完了。"):
    """感知判断替身：按顺序判断这些人会察觉到什么。"""

    async def plan(_input):
        for who, what in judgments:
            await perception.someone_notices.invoke({"who": who, "what": what})
        return said

    return ScriptedAgent(plan)


def reports(*changes: str):
    """主 agent 替身的一轮：报告这些变化，把每次报告的结果留在 ``results`` 里，最后定时刻。"""
    results: list = []

    async def plan():
        for change in changes:
            results.append(await report_change.invoke({"change": change}))
        return await sets_wake()()

    plan.results = results
    return plan


async def _a_round(world, plan):
    world.runner.plan = plan
    await main_agent.on_world_message(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )
    return plan.results


# ---------------------------------------------------------------------------
# 一个变化 → 一次感知判断 → 每个会察觉的人一条告知
# ---------------------------------------------------------------------------


async def test_a_reported_change_is_judged_once_and_each_judged_recipient_is_told_once(world):
    judge = judges(("ayana", "你听见楼下的门响了一声。"), ("akao", "厨房的灯闪了一下。"))
    world.agents[perception.PERCEPTION.prompt_id] = judge
    world.open_inboxes = {"ayana"}

    [result] = await _a_round(world, reports("楼下的门被风吹得响了一声。"))

    assert len(judge.inputs) == 1
    assert "楼下的门被风吹得响了一声。" in judge.inputs[0]
    assert world.sent == [
        {"sender": "world", "recipient": "ayana", "body": "你听见楼下的门响了一声。"},
        {"sender": "world", "recipient": "akao", "body": "厨房的灯闪了一下。"},
    ]
    assert "ayana" in result and "送达了" in result
    assert "akao" in result and "没有送达" in result and "没有开设收件箱" in result


async def test_judging_the_same_person_twice_tells_them_once_with_the_last_judgment(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(
        ("ayana", "先这么想。"), ("ayana", "改成这样。")
    )

    await _a_round(world, reports("下雨了。"))

    assert [(s["recipient"], s["body"]) for s in world.sent] == [("ayana", "改成这样。")]


async def test_a_change_nobody_notices_tells_nobody(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges()

    [result] = await _a_round(world, reports("后院的一片叶子落了。"))

    assert world.sent == []
    assert "没有人" in result


async def test_each_change_is_its_own_perception_call(world):
    judge = judges(("ayana", "x"))
    world.agents[perception.PERCEPTION.prompt_id] = judge

    await _a_round(world, reports("一。", "二。"))

    assert len(judge.inputs) == 2
    assert len([c for c in world.costs if c["round_id"].startswith("world-perception:")]) == 2


async def test_perception_runs_with_its_own_prompt_trace_and_the_sources_tools(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges()

    await _a_round(world, reports("起风了。"))

    [config] = [c for c, _ in world.built if c.prompt_id == "world_perception"]
    assert config.trace_name == "world-perception"
    expected = [t.name for t in await query_tools()] + ["someone_notices"]
    assert tools_built_for(world, "world_perception") == expected


# ---------------------------------------------------------------------------
# 判断本身的规矩
# ---------------------------------------------------------------------------


async def test_a_judgment_naming_world_itself_or_an_unusable_name_is_refused(world):
    async def plan(_input):
        plan.answers = [
            await perception.someone_notices.invoke({"who": "world", "what": "x"}),
            await perception.someone_notices.invoke({"who": "has space", "what": "x"}),
            await perception.someone_notices.invoke({"who": "ayana", "what": "  "}),
        ]
        return "好。"

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(plan)

    await _a_round(world, reports("下雪了。"))

    assert world.sent == []
    assert all(a.get("kind") == "invalid_args" for a in plan.answers)


# ---------------------------------------------------------------------------
# 告知居民只有感知判断这一条路
# ---------------------------------------------------------------------------


async def test_the_main_agent_has_no_tool_that_messages_anyone(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges()

    await _a_round(world, reports())

    main_tools = tools_built_for(world, main_agent.ROUND.prompt_id)
    assert main_tools == [t.name for t in await query_tools()] + [t.name for t in ACTIONS]
    assert [t.name for t in ACTIONS] == [
        "write_record",
        "wake_me_at",
        "report_change",
        "let_npc_appear",
    ]


def test_in_worlds_code_only_perception_sends_to_others_and_only_wake_schedules():
    """world 的代码里，用通信机制往外发的只有两处：感知判断发告知，醒来规则给自己排醒来。"""
    root = Path(app.world.__file__).parent
    senders: dict[str, set[str]] = {}
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "app.messaging.sending"
            ):
                senders.setdefault(path.relative_to(root).as_posix(), set()).update(
                    a.name for a in node.names
                )

    assert senders == {"perception.py": {"send"}, "wake.py": {"send_at"}}
