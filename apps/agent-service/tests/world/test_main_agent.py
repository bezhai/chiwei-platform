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

from .conftest import LANE, self_message, sets_nothing, sets_wake


def _stimulus(world_handle, run: int = -1) -> str:
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
    stimulus = _stimulus(world)
    assert "operator" in stimulus and "有人把窗户打开了。" in stimulus
    assert world.loaded_key == f"world:{LANE}"


async def test_its_own_time_wakes_it(world):
    current = await wake.set_next_wake(now_cst(), "该看看外面了。")
    world.scheduled.clear()

    await main_agent.on_world_message(self_message(current.message_id, "该看看外面了。"))

    assert len(world.runner.runs) == 1
    stimulus = _stimulus(world)
    assert "你给自己排的一次醒来" in stimulus and "该看看外面了。" in stimulus


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

    stimulus = _stimulus(world)
    assert "傍晚再看。" in stimulus
    assert "重新定" in stimulus
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
    stimulus = world.runner.runs[0][-1]
    assert commit["messages"][-2:] == [stimulus, commit["messages"][-1]]
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
# 模型和参数走 Dynamic Config
# ---------------------------------------------------------------------------


async def test_model_and_tool_budget_come_from_dynamic_config(world, monkeypatch):
    from inner_shared.dynamic_config import dynamic_config

    values = {main_agent.WORLD_MODEL_KEY: "some-model"}
    ints = {main_agent.WORLD_RECURSION_LIMIT_KEY: 20}
    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": values.get(k, default))
    monkeypatch.setattr(dynamic_config, "get_int", lambda k, default=0: ints.get(k, default))

    await main_agent.on_world_message(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    config = world.runner.configs[0]
    assert (config.prompt_id, config.model_id, config.recursion_limit) == (
        main_agent.ROUND_PROMPT_ID,
        "some-model",
        20,
    )


async def test_without_dynamic_config_the_code_defaults_apply(world, monkeypatch):
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(dynamic_config, "get", lambda k, default="": default)
    monkeypatch.setattr(dynamic_config, "get_int", lambda k, default=0: default)

    config = await main_agent.round_config()

    assert config.model_id == main_agent.DEFAULT_WORLD_MODEL
    assert config.recursion_limit == main_agent.DEFAULT_WORLD_RECURSION_LIMIT
