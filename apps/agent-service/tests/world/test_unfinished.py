"""告知收不回来：判断出来的告知先连同消息 id 记下，再按这个 id 发；下一轮在模型开始之前按原 id
把记下的告知再发一遍（接收方按 id 去重），然后才把"已经发生的事"摆到主 agent 眼前。

逐个看出事的位置：判断时、记下之后发之前、两次发送之间（进程死了 / 被取消）、全发完之后
存上下文之前、存下上下文之后清空之前、清空之后；以及发送本身出错。

模型换成替身（主 agent 在一轮的 context 里真调 ``report_change`` / ``let_npc_appear``），通信
机制的 ``send`` / ``send_at``、上下文存储换成替身。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.messaging.message import Kind, SendFailed, new_message
from app.world import main_agent, npc, perception, unfinished, wake
from app.world.actions import let_npc_appear, report_change

from .conftest import LANE, ScriptedAgent, sets_wake


class Crash(BaseException):
    """进程在这一步死了：什么都接不住它。"""


def judges(*judgments: tuple):
    """每条是 (谁, 察觉到什么)，或者再加一项要不要现在就让他注意到（不写就是要）。"""

    async def plan(_input):
        for who, what, *right_away in judgments:
            await perception.someone_notices.invoke(
                {"who": who, "what": what, "right_away": right_away[0] if right_away else True}
            )
        return "判断完了。"

    return ScriptedAgent(plan)


def reports(change: str, *, sets: bool = True):
    async def plan():
        await report_change.invoke({"change": change})
        if sets:
            return await sets_wake()()
        return "报告完了，忘了定时刻。"

    return plan


def watches_sends_then_sets_wake(world, seen: list):
    """一轮的替身：记下模型开始那一刻已经发出去了几条，然后定时刻。"""

    async def plan():
        seen.append(len(world.sent))
        return await sets_wake()()

    return plan


def crash_on_send(n: int, error: BaseException):
    """第 ``n`` 次发送（从 1 数）还没发出去时抛 ``error``。"""
    calls = {"n": 0}

    def hook(_recipient):
        calls["n"] += 1
        if calls["n"] == n:
            raise error

    return hook


def _message(body: str = "x"):
    return new_message(sender="operator", recipient="world", body=body, kind=Kind.MESSAGE)


def _round_input(world, run: int = -1) -> str:
    return world.runner.runs[run][-1].content


def _kept_ids() -> list[str]:
    return [n.message_id for h in unfinished.read() for n in h.notices]


TWO = (("ayana", "你听见楼下的门响了一声。"), ("akao", "厨房的窗被风吹开了。"))


# ---------------------------------------------------------------------------
# 先记下，再按记下的 id 发
# ---------------------------------------------------------------------------


async def test_the_judged_notices_are_kept_with_their_ids_before_any_is_sent(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)
    kept_at_first_send: list = []
    world.before_send = lambda _r: kept_at_first_send.append(unfinished.read()) if not kept_at_first_send else None
    world.runner.plan = reports("楼下的门被风吹得响了一声。")

    await world.deliver(_message())

    [[happening]] = kept_at_first_send
    assert "楼下的门被风吹得响了一声。" in happening.what
    assert [(n.who, n.what) for n in happening.notices] == list(TWO)
    assert world.sent_ids == [n.message_id for n in happening.notices]
    assert len(set(world.sent_ids)) == 2


# ---------------------------------------------------------------------------
# 出事的位置
# ---------------------------------------------------------------------------


async def test_dying_while_judging_leaves_nothing_kept_and_nothing_to_resend(world):
    async def dies(_input):
        raise Crash()

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(dies)
    world.runner.plan = reports("下雨了。")

    with pytest.raises(Crash):
        await world.deliver(_message())

    assert unfinished.read() == [] and world.sent == []
    world.runner.plan = sets_wake()
    await world.deliver(_message())
    assert world.sent == []
    assert "已经发生" not in _round_input(world)


async def test_a_judgment_that_fails_is_handed_back_and_nothing_is_kept_or_sent(world):
    """判断那一段没成（模型调用出错）：交回给主 agent，这一轮照常跑下去，什么都没记、没发。"""

    async def breaks(_input):
        raise RuntimeError("model timed out")

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(breaks)
    results: list = []

    async def plan():
        results.append(await report_change.invoke({"change": "下雨了。"}))
        return await sets_wake()()

    world.runner.plan = plan
    await world.deliver(_message())

    assert "没有报告出去" in results[0] and "RuntimeError" in results[0]
    assert unfinished.read() == [] and world.sent == []
    assert len(world.committed) == 1


async def test_dying_after_keeping_but_before_sending_sends_them_before_the_next_model_runs(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)
    world.before_send = crash_on_send(1, Crash())
    world.runner.plan = reports("楼下的门被风吹得响了一声。")
    trigger = _message()

    with pytest.raises(Crash):
        await world.deliver(trigger)
    assert world.sent == []
    kept = _kept_ids()

    world.before_send = None
    seen: list = []
    world.runner.plan = watches_sends_then_sets_wake(world, seen)
    await world.deliver(trigger)

    assert seen == [2]
    assert world.sent_ids == kept
    retried = _round_input(world)
    assert "已经发生" in retried and "楼下的门被风吹得响了一声。" in retried
    assert "你听见楼下的门响了一声。" in retried and "厨房的窗被风吹开了。" in retried


@pytest.mark.parametrize("interruption", [Crash(), asyncio.CancelledError()])
async def test_interrupted_between_two_sends_both_go_out_again_with_their_original_ids(
    world, interruption
):
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)
    world.before_send = crash_on_send(2, interruption)
    world.runner.plan = reports("楼下的门被风吹得响了一声。")
    trigger = _message()

    with pytest.raises(type(interruption)):
        await world.deliver(trigger)
    first_id, second_id = _kept_ids()
    assert world.sent_ids == [first_id]

    world.before_send = None
    world.runner.plan = sets_wake()
    await world.deliver(trigger)

    # ayana 那条按原 id 再发一次，接收方按 id 去重；akao 那条第一次发出。没有新 id。
    assert world.sent_ids == [first_id, first_id, second_id]
    assert "已经发生" in _round_input(world)
    # 重来的那一轮没有再报告：感知判断只跑过一次。
    assert len(world.agents[perception.PERCEPTION.prompt_id].inputs) == 1


async def test_all_sent_but_the_round_not_stored_resends_them_once_and_then_forgets(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)
    world.runner.plan = reports("楼下的门被风吹得响了一声。", sets=False)
    trigger = _message()

    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(trigger)
    kept = _kept_ids()

    world.runner.plan = sets_wake()
    await world.deliver(trigger)
    await world.deliver(_message("下一条"))

    assert world.sent_ids == kept + kept
    assert "已经发生" in _round_input(world, -2)
    assert "已经发生" not in _round_input(world, -1)
    assert unfinished.read() == []


async def test_stored_but_not_cleared_resends_them_once_more_and_clears_after(
    world, monkeypatch
):
    """存下上下文之后、清空之前死了：再发一遍（接收方按 id 去重），这一轮存下之后清空。"""
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)
    real_clear = unfinished.clear
    dies_once = {"left": 1}

    def clear():
        if dies_once["left"]:
            dies_once["left"] -= 1
            raise Crash()
        real_clear()

    monkeypatch.setattr(unfinished, "clear", clear)

    async def reports_and_says_so():
        told = await report_change.invoke({"change": "下雨了。"})
        await sets_wake()()
        return f"报告了下雨，结果是：{told}"

    world.runner.plan = reports_and_says_so
    with pytest.raises(Crash):
        await world.deliver(_message())
    assert len(world.committed) == 1
    kept = _kept_ids()

    world.runner.plan = sets_wake()
    await world.deliver(_message())

    # 这一轮读回来的上下文里已经有上一轮（它存下了），眼前也摆着补发的结果：两边都看得到。
    *history, round_input = world.runner.runs[-1]
    assert any("报告了下雨，结果是" in str(turn.content) for turn in history)
    assert "已经发生" in round_input.content and "下雨了。" in round_input.content
    # 这时上一轮的经过就在它的上下文里，眼前那段话不能说"不在"。
    assert "不在你的上下文里" not in round_input.content
    assert world.sent_ids == kept + kept
    assert unfinished.read() == []


async def test_cleared_then_scheduling_fails_leaves_nothing_to_resend(world, monkeypatch):
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)

    async def broken(**kw):
        raise SendFailed("broker did not confirm", message_id=kw["message_id"])

    monkeypatch.setattr(wake, "send_at", broken)
    world.runner.plan = reports("下雨了。")

    with pytest.raises(SendFailed):
        await world.deliver(_message())

    assert len(world.committed) == 1
    assert unfinished.read() == []


# ---------------------------------------------------------------------------
# 发送本身出错
# ---------------------------------------------------------------------------


async def test_a_send_error_fails_the_round_and_the_notice_goes_out_on_the_next(world):
    """记下来的告知必须发出去：发送出错不吞掉，这一轮按失败重来，重来时按原 id 再发。"""
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)
    world.send_fails = {"akao"}
    world.runner.plan = reports("楼下的门被风吹得响了一声。")
    trigger = _message()

    with pytest.raises(SendFailed):
        await world.deliver(trigger)
    first_id, second_id = _kept_ids()
    assert world.sent_ids == [first_id]
    assert world.committed == []

    world.send_fails = set()
    world.runner.plan = sets_wake()
    await world.deliver(trigger)

    assert world.sent_ids == [first_id, first_id, second_id]


async def test_a_send_error_while_resending_fails_the_round_before_the_model_runs(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)
    world.runner.plan = reports("下雨了。", sets=False)
    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(_message())
    kept = unfinished.read()
    runs = len(world.runner.runs)

    world.send_fails = {"akao"}
    with pytest.raises(SendFailed):
        await world.deliver(_message())

    assert len(world.runner.runs) == runs
    assert unfinished.read() == kept


# ---------------------------------------------------------------------------
# 别的情形
# ---------------------------------------------------------------------------


async def test_a_round_woken_by_something_else_resends_them_too(world):
    """那条消息重试用完进了死信，下一轮由别的消息叫醒：照样补发、照样摆出来。"""
    world.agents[perception.PERCEPTION.prompt_id] = judges(*TWO)
    world.runner.plan = reports("下雨了。", sets=False)
    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(_message("先来的"))
    kept = _kept_ids()

    world.runner.plan = sets_wake()
    await world.deliver(_message("后来的"))

    assert world.sent_ids == kept + kept
    assert "下雨了。" in _round_input(world)


async def test_an_npcs_appearance_is_kept_with_its_own_words_and_resent(world):
    async def plays(_input):
        return "门卫抬头说：「今天关门早。」"

    world.agents[npc.NPC.prompt_id] = ScriptedAgent(plays)
    world.agents[perception.PERCEPTION.prompt_id] = judges(("ayana", "门卫说今天关门早。"))
    world.before_send = crash_on_send(1, Crash())

    async def plan():
        await let_npc_appear.invoke({"npc": "门卫", "situation": "放学时的校门口。"})
        return await sets_wake()()

    world.runner.plan = plan
    with pytest.raises(Crash):
        await world.deliver(_message())
    [happening] = unfinished.read()
    assert "门卫" in happening.what and "今天关门早" in happening.what

    world.before_send = None
    world.runner.plan = sets_wake()
    await world.deliver(_message())

    assert world.sent_ids == [happening.notices[0].message_id]
    assert "今天关门早" in _round_input(world)


async def test_a_change_nobody_notices_is_still_kept_so_it_is_not_reported_again(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges()
    world.runner.plan = reports("后院落了一片叶子。", sets=False)

    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(_message())
    world.runner.plan = sets_wake()
    await world.deliver(_message())

    assert world.sent == []
    assert "后院落了一片叶子。" in _round_input(world)


async def test_every_change_a_failed_round_reported_is_kept_in_order(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(("ayana", "x"))

    async def plan():
        await report_change.invoke({"change": "先起风。"})
        await report_change.invoke({"change": "后下雨。"})
        return "忘了定时刻。"

    world.runner.plan = plan
    with pytest.raises(main_agent.NoNextWake):
        await world.deliver(_message())

    assert ["先起风。" in h.what for h in unfinished.read()] == [True, False]
    assert ["后下雨。" in h.what for h in unfinished.read()] == [False, True]


# ---------------------------------------------------------------------------
# 要不要叫醒收件人：跟告知一起记下，补发时原样带着
# ---------------------------------------------------------------------------


async def test_whether_each_notice_wakes_is_kept_and_a_resend_after_a_restart_keeps_it(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(
        ("ayana", "楼下有人喊你。", True), ("akao", "窗外的雨小了一点。", False)
    )
    world.before_send = crash_on_send(1, Crash())
    world.runner.plan = reports("楼下有人在喊，雨也小了。")

    with pytest.raises(Crash):
        await world.deliver(_message())
    [happening] = unfinished.read()
    assert [(n.who, n.wakes_recipient) for n in happening.notices] == [
        ("ayana", True),
        ("akao", False),
    ]

    world.before_send = None
    world.runner.plan = sets_wake()
    await world.deliver(_message())

    assert [s["recipient"] for s in world.sent] == ["ayana", "akao"]
    assert world.sent_wakes == [True, False]
    assert world.sent_ids == [n.message_id for n in happening.notices]


async def test_a_file_an_older_version_wrote_is_resent_as_waking(world, volume):
    """旧版本写下的告知没有"要不要叫醒"这一项：照常补发，按叫醒，不把整份文件挪开。"""
    path = volume / LANE / "unfinished.json"
    path.write_text(
        json.dumps(
            [
                {
                    "at": "2026-10-02T09:00:00+08:00",
                    "what": "你报告了一个变化：下雨了。",
                    "notices": [{"who": "ayana", "what": "下雨了。", "message_id": "kept-1"}],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    world.runner.plan = sets_wake()

    await world.deliver(_message())

    assert (world.sent_ids, world.sent_wakes) == (["kept-1"], [True])
    assert _set_aside(volume) == []
    assert "下雨了。" in _round_input(world)


# ---------------------------------------------------------------------------
# 读不出来的文件：挪到旁边留给人，不当成"没有"，也不被覆盖、删掉
# ---------------------------------------------------------------------------


def _half_broken() -> str:
    """一条好好的记录，加一条少了字段的。"""
    return json.dumps(
        [
            {
                "at": "2026-10-02T09:00:00+08:00",
                "what": "你报告了一个变化：下雨了。",
                "notices": [{"who": "ayana", "what": "下雨了。", "message_id": "kept-1"}],
            },
            {"at": "2026-10-02T09:05:00+08:00", "what": "少了 notices。"},
        ],
        ensure_ascii=False,
    )


def _garbled_wake() -> str:
    """一条告知的"要不要叫醒"写坏了：不是 true / false。"""
    return json.dumps(
        [
            {
                "at": "2026-10-02T09:00:00+08:00",
                "what": "你报告了一个变化：下雨了。",
                "notices": [
                    {
                        "who": "ayana",
                        "what": "下雨了。",
                        "message_id": "kept-1",
                        "wakes_recipient": "no",
                    }
                ],
            },
        ],
        ensure_ascii=False,
    )


def _set_aside(volume) -> list:
    return sorted((volume / LANE).glob("unfinished.json.unreadable-*"))


@pytest.mark.parametrize(
    "content",
    [
        _half_broken().encode("utf-8"),
        _garbled_wake().encode("utf-8"),
        "不是 JSON".encode(),
        b"\xff\xfe broken utf-8",
    ],
)
async def test_an_unreadable_file_is_set_aside_logged_and_the_round_goes_on(
    world, volume, caplog, content
):
    path = volume / LANE / "unfinished.json"
    path.write_bytes(content)
    world.agents[perception.PERCEPTION.prompt_id] = judges(("akao", "起风了。"))
    world.runner.plan = reports("起风了。")

    with caplog.at_level("ERROR", logger="app.world.unfinished"):
        await world.deliver(_message())

    # 这一轮照常跑完：存下了上下文，自己报告的那条照常发，记着的那条没有被当成"没有"补发。
    assert len(world.committed) == 1
    assert [s["recipient"] for s in world.sent] == ["akao"]
    # 原样留在旁边，这一轮清空 unfinished 的时候也没有碰它。
    [aside] = _set_aside(volume)
    assert aside.read_bytes() == content
    assert not path.exists()
    assert any(str(aside) in r.getMessage() for r in caplog.records)


async def test_noting_over_an_unreadable_file_sets_it_aside_instead_of_overwriting_it(
    world, volume
):
    path = volume / LANE / "unfinished.json"
    path.write_text(_half_broken(), encoding="utf-8")

    unfinished.note("你报告了一个变化：天晴了。", [])

    [aside] = _set_aside(volume)
    assert aside.read_text(encoding="utf-8") == _half_broken()
    assert [h.what for h in unfinished.read()] == ["你报告了一个变化：天晴了。"]
