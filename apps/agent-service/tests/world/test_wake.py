"""world 的醒来规则：下次醒来的时刻写在私有状态里，自定消息只认状态里记的那一条。

通信机制换成替身（它自己的契约在 ``tests/messaging`` 里用真 broker 验过），这里看的是
world 这一侧：先写状态再排消息、旧的自定消息怎么认出来、进程启动时补不补醒。
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.infra.cst_time import now_cst
from app.messaging.message import Kind, SendFailed, new_message
from app.world import wake

from .conftest import LANE


@pytest.fixture
def scheduled(monkeypatch) -> list[dict]:
    """记下每一次排给自己的定时消息；排的那一刻私有状态里记的是什么也一起记下。"""
    seen: list[dict] = []

    async def send_at(**kw):
        seen.append({**kw, "state_then": wake.read_state()})
        return kw["message_id"]

    monkeypatch.setattr(wake, "send_at", send_at)
    return seen


def _state_file(volume):
    return volume / LANE / "next_wake.json"


async def test_setting_the_next_wake_records_it_then_schedules_that_very_message(
    volume, scheduled
):
    at = now_cst() + timedelta(hours=2)

    chosen = await wake.set_next_wake(at, "两小时后看看雨停了没有。")

    assert wake.read_next_wake() == chosen
    assert (chosen.at, chosen.reason) == (at, "两小时后看看雨停了没有。")
    assert len(scheduled) == 1
    sent = scheduled[0]
    assert (sent["sender"], sent["recipient"]) == ("world", "world")
    assert (sent["at"], sent["message_id"]) == (at, chosen.message_id)
    assert sent["body"] == "两小时后看看雨停了没有。"
    assert sent["state_then"] == wake.WakeState(current=None, pending=chosen), (
        "排消息之前，新时刻要先作为待定写进状态"
    )
    on_disk = json.loads(_state_file(volume).read_text())
    assert on_disk["current"]["message_id"] == chosen.message_id
    assert on_disk["pending"] is None


async def test_when_scheduling_fails_the_new_wake_stays_pending(volume, monkeypatch):
    """排消息失败时新时刻留在"待定"，"当前"不变：这一轮会失败重来；进程要是死了，重启时补排。"""

    async def broken(**kw):
        raise SendFailed("broker did not confirm", message_id=kw["message_id"])

    monkeypatch.setattr(wake, "send_at", broken)
    at = now_cst() + timedelta(hours=1)

    with pytest.raises(SendFailed):
        await wake.set_next_wake(at, "一小时后。")

    assert wake.read_next_wake() is None
    assert wake.read_state().pending.at == at


async def test_only_the_wake_named_in_the_state_is_current(volume, scheduled):
    first = await wake.set_next_wake(now_cst() + timedelta(hours=1), "第一次定的。")
    second = await wake.set_next_wake(now_cst() + timedelta(hours=3), "后来改定的。")

    def self_message(message_id):
        return new_message(
            sender="world", recipient="world", body="x", kind=Kind.MESSAGE, message_id=message_id
        )

    assert wake.is_stale_wake(self_message(first.message_id))
    assert not wake.is_stale_wake(self_message(second.message_id))


async def test_messages_from_others_and_notices_are_never_stale_wakes(volume, scheduled):
    await wake.set_next_wake(now_cst() + timedelta(hours=1), "定了。")

    from_operator = new_message(
        sender="operator", recipient="world", body="有人推门。", kind=Kind.MESSAGE
    )
    notice = new_message(
        sender="world", recipient="world", body="没有送达。", kind=Kind.NOT_DELIVERED
    )

    assert not wake.is_stale_wake(from_operator)
    assert not wake.is_stale_wake(notice)


async def test_without_any_state_every_self_message_is_stale(volume):
    message = new_message(sender="world", recipient="world", body="x", kind=Kind.MESSAGE)

    assert wake.read_next_wake() is None
    assert wake.is_stale_wake(message)


# ---------------------------------------------------------------------------
# 进程启动时补醒
# ---------------------------------------------------------------------------


async def test_on_start_without_a_wake_it_wakes_right_away(volume, scheduled):
    before = now_cst()

    await wake.wake_on_start()

    assert len(scheduled) == 1
    armed = wake.read_next_wake()
    assert scheduled[0]["message_id"] == armed.message_id
    assert before <= scheduled[0]["at"] <= now_cst()
    assert "没有" in scheduled[0]["body"]


async def test_on_start_with_a_wake_already_past_it_wakes_right_away(volume, scheduled):
    missed = await wake.set_next_wake(now_cst() - timedelta(minutes=30), "错过的那次。")
    scheduled.clear()

    await wake.wake_on_start()

    armed = wake.read_next_wake()
    assert armed.message_id != missed.message_id
    assert armed.at <= now_cst()
    assert scheduled[0]["message_id"] == armed.message_id
    assert "已经过了" in scheduled[0]["body"]
    assert "错过的那次。" in scheduled[0]["body"]


async def test_on_start_with_a_wake_still_ahead_it_does_not_wake_now(volume, scheduled):
    """时刻未到：不立刻醒。同一条消息按原 id、原时刻再排一次——死在"写好状态、还没排
    消息"之间的话，这是那条消息唯一的来源；原来那条要是还在，接收方按 id 去重。"""
    ahead = await wake.set_next_wake(now_cst() + timedelta(hours=5), "五小时后。")
    scheduled.clear()

    await wake.wake_on_start()

    assert wake.read_next_wake() == ahead
    assert len(scheduled) == 1
    assert (scheduled[0]["message_id"], scheduled[0]["at"], scheduled[0]["body"]) == (
        ahead.message_id,
        ahead.at,
        "五小时后。",
    )


async def test_an_unreadable_state_counts_as_no_wake(volume, scheduled):
    _state_file(volume).parent.mkdir(parents=True)
    _state_file(volume).write_text("{not json", encoding="utf-8")

    assert wake.read_next_wake() is None
    await wake.wake_on_start()

    assert len(scheduled) == 1
    assert wake.read_next_wake().message_id == scheduled[0]["message_id"]


# ---------------------------------------------------------------------------
# 一轮最终进了死信
# ---------------------------------------------------------------------------


@pytest.fixture
def backoff_minutes(monkeypatch):
    """Dynamic Config 的替身：默认什么都没配；测试可以往 ``values`` 里放值。"""
    from inner_shared.dynamic_config import dynamic_config

    values: dict[str, int] = {}
    monkeypatch.setattr(
        dynamic_config, "get_int", lambda key, default=0: values.get(key, default)
    )
    return values


def _self(message_id):
    return new_message(
        sender="world", recipient="world", body="x", kind=Kind.MESSAGE, message_id=message_id
    )


async def test_the_due_wake_failing_for_good_arms_one_wake_after_the_backoff(
    volume, scheduled, backoff_minutes
):
    due = await wake.set_next_wake(now_cst(), "到点了。")
    scheduled.clear()

    await wake.wake_after_failure(_self(due.message_id), RuntimeError("模型一直报错"))

    after = wake.read_next_wake()
    assert after.message_id != due.message_id
    expected = now_cst() + timedelta(minutes=wake.DEFAULT_WAKE_BACKOFF_MINUTES)
    assert abs((after.at - expected).total_seconds()) < 5
    assert "模型一直报错" in after.reason
    assert [s["message_id"] for s in scheduled] == [after.message_id]


async def test_the_backoff_comes_from_dynamic_config(volume, scheduled, backoff_minutes):
    backoff_minutes[wake.WAKE_BACKOFF_MINUTES_KEY] = 5
    due = await wake.set_next_wake(now_cst(), "到点了。")

    await wake.wake_after_failure(_self(due.message_id), RuntimeError("x"))

    expected = now_cst() + timedelta(minutes=5)
    assert abs((wake.read_next_wake().at - expected).total_seconds()) < 5


@pytest.mark.parametrize("configured", [0, -3])
async def test_a_backoff_that_is_not_positive_falls_back_to_the_default(
    volume, scheduled, backoff_minutes, configured
):
    backoff_minutes[wake.WAKE_BACKOFF_MINUTES_KEY] = configured
    due = await wake.set_next_wake(now_cst(), "到点了。")

    await wake.wake_after_failure(_self(due.message_id), RuntimeError("x"))

    expected = now_cst() + timedelta(minutes=wake.DEFAULT_WAKE_BACKOFF_MINUTES)
    assert abs((wake.read_next_wake().at - expected).total_seconds()) < 5


async def test_the_backoff_wake_failing_again_backs_off_again(
    volume, scheduled, backoff_minutes
):
    due = await wake.set_next_wake(now_cst(), "到点了。")
    await wake.wake_after_failure(_self(due.message_id), RuntimeError("第一次"))
    first_backoff = wake.read_next_wake()

    await wake.wake_after_failure(_self(first_backoff.message_id), RuntimeError("第二次"))

    second_backoff = wake.read_next_wake()
    assert second_backoff.message_id != first_backoff.message_id
    assert "第二次" in second_backoff.reason


async def test_someone_elses_message_failing_for_good_leaves_the_planned_wake(
    volume, scheduled, backoff_minutes
):
    """那时私有状态里原定的下次醒来还在，world 本来就会按时醒：不另排。"""
    planned = await wake.set_next_wake(now_cst() + timedelta(hours=2), "原定的。")
    scheduled.clear()
    from_operator = new_message(
        sender="operator", recipient="world", body="有人敲门。", kind=Kind.MESSAGE
    )

    await wake.wake_after_failure(from_operator, RuntimeError("x"))

    assert scheduled == []
    assert wake.read_state() == wake.WakeState(current=planned)


async def test_a_replaced_wake_failing_for_good_does_nothing(volume, scheduled, backoff_minutes):
    replaced = await wake.set_next_wake(now_cst(), "被取代的。")
    current = await wake.set_next_wake(now_cst() + timedelta(hours=1), "后来定的。")
    scheduled.clear()

    await wake.wake_after_failure(_self(replaced.message_id), RuntimeError("x"))

    assert scheduled == []
    assert wake.read_next_wake() == current
