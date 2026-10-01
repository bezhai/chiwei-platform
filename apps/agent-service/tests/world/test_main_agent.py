"""主 agent 的一轮：什么叫醒它、它眼前摆着什么、一轮怎样才算跑完。

模型换成替身（在这一轮的 context 里真调工具），上下文存储、成本记账、通信机制的定时发送
换成替身。醒来规则的另一半（私有状态、启动补醒）在 ``test_wake.py``，跑在真 broker 上的
整条链在 ``test_wakes_on_a_real_broker.py``。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.infra.cst_time import now_cst
from app.messaging.message import Kind, new_message
from app.world import main_agent, wake

from .conftest import LANE, self_message, sets_nothing, sets_wake, tools_built_for


def _round_input(world_handle, run: int = -1) -> str:
    return world_handle.runner.runs[run][-1].content


# ---------------------------------------------------------------------------
# 两种醒来原因
# ---------------------------------------------------------------------------


async def test_a_message_from_someone_wakes_it(world):
    message = new_message(
        sender="operator", recipient="world", body="有人把窗户打开了。", kind=Kind.MESSAGE
    )

    await main_agent.on_world_message(message)

    assert len(world.runner.runs) == 1
    round_input = _round_input(world)
    assert "operator" in round_input and "有人把窗户打开了。" in round_input
    assert world.loaded_key == f"world:{LANE}"


async def test_its_own_time_wakes_it(world):
    current = await wake.set_next_wake(now_cst(), "该看看外面了。")
    world.scheduled.clear()

    await main_agent.on_world_message(self_message(current.message_id, "该看看外面了。"))

    assert len(world.runner.runs) == 1
    round_input = _round_input(world)
    assert "你给自己排的一次醒来" in round_input and "该看看外面了。" in round_input


async def test_a_wake_replaced_by_a_later_one_is_skipped_without_a_round(world):
    replaced = await wake.set_next_wake(now_cst() + timedelta(hours=1), "第一次定的。")
    current = await wake.set_next_wake(now_cst() + timedelta(hours=4), "后来改的。")
    world.scheduled.clear()

    await main_agent.on_world_message(self_message(replaced.message_id))

    assert world.runner.runs == []
    assert world.scheduled == [] and world.committed == []
    assert wake.read_next_wake() == current


async def test_woken_by_someone_else_it_sees_the_wake_it_had_planned(world):
    planned = await wake.set_next_wake(now_cst() + timedelta(hours=6), "傍晚再看。")

    await main_agent.on_world_message(
        new_message(sender="operator", recipient="world", body="下雨了。", kind=Kind.MESSAGE)
    )

    round_input = _round_input(world)
    assert "傍晚再看。" in round_input
    assert "重新定" in round_input
    assert wake.read_next_wake() != planned


# ---------------------------------------------------------------------------
# 每一轮最后必须定下次醒来的时刻
# ---------------------------------------------------------------------------


async def test_a_round_ends_by_recording_scheduling_and_remembering(world):
    await main_agent.on_world_message(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    chosen = wake.read_next_wake()
    assert chosen is not None and chosen.at > now_cst()
    assert "过一阵再看看。" in chosen.reason
    assert [(s["at"], s["message_id"]) for s in world.scheduled] == [
        (chosen.at, chosen.message_id)
    ]
    [commit] = world.committed
    assert commit["key"] == f"world:{LANE}"
    assert commit["expected_ver"] == 3
    round_input = world.runner.runs[0][-1]
    assert commit["messages"][-2:] == [round_input, commit["messages"][-1]]
    assert commit["messages"][-1].content == "这一轮看完了。"
    assert world.costs[0]["actor"] == "world"


async def test_a_round_that_sets_no_next_wake_fails_and_changes_nothing(world):
    before = await wake.set_next_wake(now_cst() + timedelta(hours=1), "原来定的。")
    world.scheduled.clear()
    world.runner.plan = sets_nothing()

    with pytest.raises(main_agent.NoNextWake):
        await main_agent.on_world_message(
            new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
        )

    assert world.committed == []
    assert world.scheduled == []
    assert wake.read_next_wake() == before


async def test_a_failed_round_woken_by_its_own_time_runs_again_on_retry(world):
    """失败按通信机制重试：同一条自定消息再来，它仍是状态里记的那条，这一轮重跑。"""
    current = await wake.set_next_wake(now_cst(), "到点了。")
    world.runner.plan = sets_nothing()
    trigger = self_message(current.message_id, "到点了。")

    with pytest.raises(main_agent.NoNextWake):
        await main_agent.on_world_message(trigger)
    world.runner.plan = sets_wake()
    await main_agent.on_world_message(trigger)

    assert len(world.runner.runs) == 2
    assert wake.read_next_wake().message_id != current.message_id


async def test_when_the_context_cannot_be_stored_the_round_fails_before_scheduling(
    world, monkeypatch
):
    from app.agent.continuity import TranscriptConflict

    async def conflict(*a, **kw):
        raise TranscriptConflict("别人写过了")

    monkeypatch.setattr(main_agent, "commit_transcript", conflict)

    with pytest.raises(TranscriptConflict):
        await main_agent.on_world_message(
            new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
        )

    assert world.scheduled == []
    assert wake.read_next_wake() is None


# ---------------------------------------------------------------------------
# 它手里有什么
# ---------------------------------------------------------------------------


async def test_the_main_agent_gets_every_enabled_sources_query_tools_and_its_own_actions(world):
    from app.world.actions import ACTIONS
    from app.world.sources import query_tools

    await main_agent.on_world_message(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    expected = [t.name for t in await query_tools()] + [t.name for t in ACTIONS]
    assert tools_built_for(world, main_agent.ROUND.prompt_id) == expected
    assert {"list_records", "read_record", "check_weather", "search_web"} <= set(expected)


async def test_the_round_runs_as_its_own_trace_with_its_own_prompt(world):
    await main_agent.on_world_message(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    config = world.runner.configs[0]
    assert (config.prompt_id, config.trace_name) == ("world_round", "world-round")
    assert world.costs[0]["round_id"].startswith("world-round:")


async def test_what_the_sources_return_is_trimmed_as_material(world, monkeypatch):
    from app.world import sources

    seen = {}
    real = main_agent.trim_for_round

    def trim(history, **kw):
        seen.update(kw)
        return real(history, **kw)

    monkeypatch.setattr(main_agent, "trim_for_round", trim)

    await main_agent.on_world_message(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    assert seen["material_tools"] == sources.material_tools()
    assert "write_record" not in seen["material_tools"]
