"""一轮没跑完时已经发生的事：告知收不回来，重来的那一轮要看得见它们，才不会把同一个变化再报告一遍。

模型换成替身（主 agent 在一轮的 context 里真调 ``report_change`` / ``let_npc_appear``），通信
机制的 ``send`` / ``send_at``、上下文存储换成替身。
"""
from __future__ import annotations

import pytest

from app.messaging.message import Kind, SendFailed, new_message
from app.world import main_agent, npc, perception, unfinished, wake
from app.world.actions import let_npc_appear, report_change

from .conftest import LANE, ScriptedAgent, sets_wake


def judges(who: str, what: str):
    async def plan(_input):
        await perception.someone_notices.invoke({"who": who, "what": what})
        return "判断完了。"

    return ScriptedAgent(plan)


def reports_then(change: str, *, sets: bool):
    async def plan():
        await report_change.invoke({"change": change})
        if sets:
            return await sets_wake()()
        return "报告完了，忘了定时刻。"

    return plan


def _round_input(world, run: int = -1) -> str:
    return world.runner.runs[run][-1].content


def _message(body: str = "x"):
    return new_message(sender="operator", recipient="world", body=body, kind=Kind.MESSAGE)


async def test_a_retried_round_sees_the_change_its_failed_attempt_already_reported(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges("ayana", "你听见楼下的门响了一声。")
    trigger = _message()

    world.runner.plan = reports_then("楼下的门被风吹得响了一声。", sets=False)
    with pytest.raises(main_agent.NoNextWake):
        await main_agent.on_world_message(trigger)
    world.runner.plan = sets_wake()
    await main_agent.on_world_message(trigger)

    retried = _round_input(world)
    assert "已经发生" in retried
    assert "楼下的门被风吹得响了一声。" in retried
    assert "ayana" in retried and "你听见楼下的门响了一声。" in retried
    # 重来的那一轮没有再报告，居民只收到一条。
    assert len(world.sent) == 1


async def test_once_a_round_is_stored_what_happened_in_it_is_not_shown_again(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges("ayana", "下雨了。")

    world.runner.plan = reports_then("下雨了。", sets=True)
    await main_agent.on_world_message(_message())
    world.runner.plan = sets_wake()
    await main_agent.on_world_message(_message())

    assert "已经发生" not in _round_input(world)
    assert unfinished.read() == []


async def test_a_round_stored_but_not_scheduled_leaves_nothing_behind(world, monkeypatch):
    """上下文存下了才清：存下之后重来的那一轮从上下文里就看得到它报告过什么。"""
    world.agents[perception.PERCEPTION.prompt_id] = judges("ayana", "下雨了。")

    async def broken(**kw):
        raise SendFailed("broker did not confirm", message_id=kw["message_id"])

    monkeypatch.setattr(wake, "send_at", broken)
    world.runner.plan = reports_then("下雨了。", sets=True)

    with pytest.raises(SendFailed):
        await main_agent.on_world_message(_message())

    assert len(world.committed) == 1
    assert unfinished.read() == []


async def test_a_round_that_fails_before_being_stored_keeps_what_happened(world, monkeypatch):
    from app.agent.continuity import TranscriptConflict

    world.agents[perception.PERCEPTION.prompt_id] = judges("ayana", "下雨了。")

    async def conflict(*a, **kw):
        raise TranscriptConflict("别人写过了")

    monkeypatch.setattr(main_agent, "commit_transcript", conflict)
    world.runner.plan = reports_then("下雨了。", sets=True)

    with pytest.raises(TranscriptConflict):
        await main_agent.on_world_message(_message())

    [happened] = unfinished.read()
    assert "下雨了。" in happened.what and "ayana" in happened.what


async def test_every_change_a_failed_round_reported_is_kept_in_order(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges("ayana", "x")

    async def plan():
        await report_change.invoke({"change": "先起风。"})
        await report_change.invoke({"change": "后下雨。"})
        return "忘了定时刻。"

    world.runner.plan = plan
    with pytest.raises(main_agent.NoNextWake):
        await main_agent.on_world_message(_message())

    assert ["先起风。" in h.what for h in unfinished.read()] == [True, False]
    assert ["后下雨。" in h.what for h in unfinished.read()] == [False, True]


async def test_an_npcs_appearance_is_kept_with_its_own_words(world):
    async def plays(_input):
        return "门卫抬头说：「今天关门早。」"

    world.agents[npc.NPC.prompt_id] = ScriptedAgent(plays)
    world.agents[perception.PERCEPTION.prompt_id] = judges("ayana", "门卫说今天关门早。")

    async def plan():
        await let_npc_appear.invoke({"npc": "门卫", "situation": "放学时的校门口。"})
        return "出场完了，忘了定时刻。"

    world.runner.plan = plan
    with pytest.raises(main_agent.NoNextWake):
        await main_agent.on_world_message(_message())

    [happened] = unfinished.read()
    assert "门卫" in happened.what and "今天关门早" in happened.what


async def test_it_is_kept_on_worlds_volume_and_an_unreadable_file_counts_as_nothing(world, volume):
    unfinished.note("某件事。")
    path = volume / LANE / "unfinished.json"
    assert path.exists()

    path.write_text("不是 JSON", encoding="utf-8")

    assert unfinished.read() == []
