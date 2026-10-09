"""主 agent 的一轮：什么叫醒它、它眼前摆着什么、一轮怎样才算跑完。

模型换成替身（在这一轮的 context 里真调工具），上下文存储、成本记账、通信机制的定时发送
换成替身。醒来规则的另一半（私有状态、启动补醒）在 ``test_wake.py``，跑在真 broker 上的
整条链在 ``test_wakes_on_a_real_broker.py``。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.agent.continuity import CHECKPOINT_HEAD, estimate_tokens
from app.agent.neutral import Message as Turn
from app.agent.neutral import Role
from app.infra.cst_time import now_cst
from app.messaging.message import Kind, new_message
from app.world import main_agent, wake
from app.world.agents import when

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

    await world.deliver(message)

    assert len(world.runner.runs) == 1
    round_input = _round_input(world)
    assert "operator" in round_input and "有人把窗户打开了。" in round_input
    assert world.loaded_key == f"world:{LANE}"


async def test_its_own_time_wakes_it(world):
    current = await wake.set_next_wake(now_cst(), "该看看外面了。")
    world.scheduled.clear()

    await world.deliver(self_message(current.message_id, "该看看外面了。"))

    assert len(world.runner.runs) == 1
    round_input = _round_input(world)
    assert "你给自己排的一次醒来" in round_input and "该看看外面了。" in round_input


async def test_a_wake_replaced_by_a_later_one_is_skipped_without_a_round(world):
    replaced = await wake.set_next_wake(now_cst() + timedelta(hours=1), "第一次定的。")
    current = await wake.set_next_wake(now_cst() + timedelta(hours=4), "后来改的。")
    world.scheduled.clear()

    await world.deliver(self_message(replaced.message_id))

    assert world.runner.runs == []
    assert world.scheduled == [] and world.committed == []
    assert wake.read_next_wake() == current


async def test_woken_by_someone_else_it_sees_the_wake_it_had_planned(world):
    planned = await wake.set_next_wake(now_cst() + timedelta(hours=6), "傍晚再看。")

    await world.deliver(
        new_message(sender="operator", recipient="world", body="下雨了。", kind=Kind.MESSAGE)
    )

    round_input = _round_input(world)
    assert "傍晚再看。" in round_input
    assert "重新定" in round_input
    assert wake.read_next_wake() != planned


# ---------------------------------------------------------------------------
# 一轮带着几条消息：每条的发件人、时间、原文，按到达的先后
# ---------------------------------------------------------------------------


def _not_delivered(body: str):
    return new_message(sender="world", recipient="world", body=body, kind=Kind.NOT_DELIVERED)


async def test_a_round_shows_each_of_its_messages_with_sender_time_and_text_in_order(world):
    current = await wake.set_next_wake(now_cst(), "该看看外面了。")
    resident = new_message(sender="赤尾", recipient="world", body="我把窗关上了。", kind=Kind.MESSAGE)
    own = self_message(current.message_id, "该看看外面了。")
    bounced = _not_delivered("你发给千凪的消息没有送达。")

    await main_agent.run_round([resident, own, bounced])

    lines = _round_input(world).split("\n")
    assert lines[1:8] == [
        "【叫醒你的】这一轮有 3 条消息，按到达的先后：",
        f"（1）赤尾 发来一条消息（{when(resident.time)}）：",
        "我把窗关上了。",
        f"（2）你给自己排的一次醒来（排在 {when(own.time)}）：",
        "该看看外面了。",
        f"（3）通信机制告诉你，你的一条消息没有送达（{when(bounced.time)}）：",
        "你发给千凪的消息没有送达。",
    ]


async def test_a_round_that_takes_its_own_wake_is_not_told_its_planned_wake_is_replaced(world):
    current = await wake.set_next_wake(now_cst(), "该看看外面了。")
    resident = new_message(sender="赤尾", recipient="world", body="我出门了。", kind=Kind.MESSAGE)

    await main_agent.run_round([resident, self_message(current.message_id, "该看看外面了。")])

    assert "【你原来定的下次醒来】" not in _round_input(world)


async def test_a_round_of_others_messages_only_is_told_the_wake_it_had_planned(world):
    await wake.set_next_wake(now_cst() + timedelta(hours=6), "傍晚再看。")

    await main_agent.run_round(
        [
            new_message(sender="赤尾", recipient="world", body="我出门了。", kind=Kind.MESSAGE),
            _not_delivered("你发给千凪的消息没有送达。"),
        ]
    )

    round_input = _round_input(world)
    assert "【你原来定的下次醒来】" in round_input and "傍晚再看。" in round_input


# ---------------------------------------------------------------------------
# 每一轮最后必须定下次醒来的时刻
# ---------------------------------------------------------------------------


async def test_a_round_ends_by_recording_scheduling_and_remembering(world):
    await world.deliver(
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
        await world.deliver(
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
        await world.deliver(trigger)
    world.runner.plan = sets_wake()
    await world.deliver(trigger)

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
        await world.deliver(
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

    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    expected = [t.name for t in await query_tools()] + [t.name for t in ACTIONS]
    assert tools_built_for(world, main_agent.ROUND.prompt_id) == expected
    assert {
        "list_records", "read_record", "search_web",
        "check_current_weather", "check_hourly_forecast", "check_daily_forecast",
    } <= set(expected)


async def test_the_round_runs_as_its_own_trace_with_its_own_prompt(world):
    await world.deliver(
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

    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    assert seen["material_tools"] == sources.material_tools()
    assert "write_record" not in seen["material_tools"]


# ---------------------------------------------------------------------------
# 它带进一轮的历史有多长：两三万 token
# ---------------------------------------------------------------------------


def test_the_trim_policy_keeps_the_five_constraints():
    from app.agent.continuity import MAX_CLEANUP_MINUTES
    from app.world.main_agent import TRIM_POLICY as p

    assert p.material_minutes > 0 and p.own_minutes > 0
    assert p.own_minutes >= p.material_minutes
    assert 1 <= p.cleanup_minutes <= MAX_CLEANUP_MINUTES
    assert p.hard_cap_tokens > 0 and p.trim_target_tokens > 0
    assert p.trim_target_tokens < p.hard_cap_tokens


def _turns(n: int) -> list[Turn]:
    """``n`` 条刚说过的话，每条按 :func:`app.agent.continuity.estimate_tokens` 估约 1k token。"""
    return [
        Turn(
            role=Role.ASSISTANT if i % 2 else Role.USER,
            content=f"第{i}条：" + "字" * 1000,
        )
        for i in range(n)
    ]


async def test_a_history_under_30k_tokens_is_carried_into_the_round_whole(world):
    world.history = _turns(29)
    assert 29_000 < estimate_tokens(world.history) < 30_000

    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    *fed, marker, _ = world.runner.runs[0]
    assert fed == _turns(29)
    assert marker.content.startswith(CHECKPOINT_HEAD)


async def test_a_history_over_30k_tokens_is_cut_back_to_20k_before_the_round(world):
    world.history = _turns(31)
    assert estimate_tokens(world.history) > 30_000

    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )

    *fed, _ = world.runner.runs[0]
    # 从最老的丢起，一降到 2 万以下就停：留下的是最新的那一段，后面跟着这一轮插入的清理标记。
    assert 19_000 < estimate_tokens(fed) <= 20_000
    *kept, marker = fed
    assert kept == _turns(31)[-len(kept) :]
    assert marker.content.startswith(CHECKPOINT_HEAD)


def _says(text: str):
    """定好下次醒来，最后说 ``text``。"""
    wakes = sets_wake()

    async def plan():
        await wakes()
        return text

    return plan


def _checkpoints(messages: list[Turn]) -> list[Turn]:
    return [
        m
        for m in messages
        if isinstance(m.content, str) and m.content.startswith(CHECKPOINT_HEAD)
    ]


def _from_operator():
    return new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)


async def test_a_checkpoint_cut_away_on_write_back_comes_back_with_the_records_next_round(
    world, monkeypatch
):
    """同一个整点里接连跑了很多轮：存回去时硬顶把这个整点的清理标记连同记录目录裁掉了，
    下一轮再插一条同一时刻的，记录目录跟着回到眼前。"""
    from app.world import records

    at = now_cst()
    monkeypatch.setattr(main_agent, "now_cst", lambda: at)
    records.write("地方/厨房.md", "灶上炖着汤。", expected=None)
    world.history = []
    await world.deliver(_from_operator())
    [first] = _checkpoints(world.history)
    assert "地方/厨房.md" in first.content

    # 这个整点里后来又跑了很多轮，历史快到 3 万；这一轮自己再说一段，存回去时撞上硬顶。
    world.history = [*world.history, *_turns(28)]
    world.runner.plan = _says("字" * 6000)
    await world.deliver(_from_operator())

    assert first in world.runner.runs[1]
    stored = world.committed[-1]["messages"]
    assert estimate_tokens(stored) <= 20_000
    assert _checkpoints(stored) == []

    world.runner.plan = sets_wake()
    await world.deliver(_from_operator())

    *fed, again, _ = world.runner.runs[2]
    assert _checkpoints(fed) == []
    assert again.content == first.content


async def test_a_round_that_alone_runs_over_30k_is_stored_whole_and_cut_on_the_next_read(
    world,
):
    world.history = _turns(5)
    long = "字" * 31_000
    world.runner.plan = _says(long)
    await world.deliver(_from_operator())

    # 存回去时这一轮自己的输入和产出一条不丢，哪怕它们自己就超过了 3 万。
    stored = world.committed[-1]["messages"]
    assert stored == [world.runner.runs[0][-1], Turn(role=Role.ASSISTANT, content=long)]
    assert estimate_tokens(stored) > 30_000

    world.runner.plan = sets_wake()
    await world.deliver(_from_operator())

    # 下一轮读出来先裁：超过顶的那一轮整组丢掉，只剩这一轮插入的清理标记。
    [marker, _] = world.runner.runs[1]
    assert marker.content.startswith(CHECKPOINT_HEAD)
