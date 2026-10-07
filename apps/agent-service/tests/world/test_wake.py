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
        seen.append({**kw, "state_then": wake.read_next_wake()})
        return kw["message_id"]

    monkeypatch.setattr(wake, "send_at", send_at)
    return seen


def _state_file(volume):
    return volume / LANE / "next_wake.json"


async def test_setting_the_next_wake_sends_it_first_and_only_then_records_it(
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
    assert sent["state_then"] is None, "排出去之前不能先记进状态"
    on_disk = json.loads(_state_file(volume).read_text())
    assert on_disk["latest"]["message_id"] == chosen.message_id


async def test_when_sending_fails_nothing_is_recorded(volume, monkeypatch):
    """没排出去的唤醒不记进状态：状态里记的，一定是排出去了的。"""
    earlier = None

    async def broken(**kw):
        raise SendFailed("broker did not confirm", message_id=kw["message_id"])

    monkeypatch.setattr(wake, "send_at", broken)

    with pytest.raises(SendFailed):
        await wake.set_next_wake(now_cst() + timedelta(hours=1), "一小时后。")

    assert wake.read_next_wake() is earlier


async def test_only_the_wake_named_in_the_state_is_current(volume, scheduled):
    first = await wake.set_next_wake(now_cst() + timedelta(hours=1), "第一次定的。")
    second = await wake.set_next_wake(now_cst() + timedelta(hours=3), "后来改定的。")

    def self_message(message_id):
        return new_message(
            sender="world", recipient="world", body="x", kind=Kind.MESSAGE, message_id=message_id
        )

    assert wake.is_stale_wake(self_message(first.message_id))
    assert not wake.is_stale_wake(self_message(second.message_id))


def _wake_message(wake_set: wake.NextWake):
    """排出去的那条自定醒来到点送来时的样子。"""
    return new_message(
        sender="world",
        recipient="world",
        body=wake_set.reason,
        kind=Kind.MESSAGE,
        message_id=wake_set.message_id,
    )


async def test_setting_the_time_already_scheduled_sends_nothing_and_keeps_that_wake(
    volume, scheduled
):
    """这个时刻已经排着一条了：不再排新的，状态不动。那一条到点送来时仍是最新唤醒，照常醒。"""
    at = now_cst() + timedelta(hours=2)
    first = await wake.set_next_wake(at, "两小时后看看雨停了没有。")

    again = await wake.set_next_wake(at, "还是两小时后再看。")

    assert again == first
    assert wake.read_next_wake() == first
    assert len(scheduled) == 1
    assert not wake.is_stale_wake(_wake_message(first))


async def test_the_same_time_is_scheduled_again_once_its_wake_has_been_taken(volume, scheduled):
    """状态里的最新唤醒已经送到、就在这一轮带着的消息里：它不会再来，同一个时刻也要重新排一条。"""
    at = now_cst() + timedelta(hours=2)
    first = await wake.set_next_wake(at, "两小时后。")

    again = await wake.set_next_wake(at, "还是这个时刻。", taken=[_wake_message(first)])

    assert again.message_id != first.message_id
    assert wake.read_next_wake() == again
    assert [s["message_id"] for s in scheduled] == [first.message_id, again.message_id]


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


async def test_on_start_with_a_wake_due_this_very_moment_it_still_wakes_right_away(
    volume, scheduled, monkeypatch
):
    """记的时刻正好是现在：它跟"已经过了"一样立刻补醒，不能因为"同一个时刻只排一条"被省掉——
    那一条可能早就丢了，补醒就是为这种情况排的。"""
    moment = now_cst()
    monkeypatch.setattr(wake, "now_cst", lambda: moment)
    due = await wake.set_next_wake(moment, "就是现在。")
    scheduled.clear()

    await wake.wake_on_start()

    armed = wake.read_next_wake()
    assert armed.message_id != due.message_id
    assert [s["message_id"] for s in scheduled] == [armed.message_id]
    assert "已经过了" in scheduled[0]["body"]


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
    _state_file(volume).parent.mkdir(parents=True, exist_ok=True)
    _state_file(volume).write_text("{not json", encoding="utf-8")

    assert wake.read_next_wake() is None
    await wake.wake_on_start()

    assert len(scheduled) == 1
    assert wake.read_next_wake().message_id == scheduled[0]["message_id"]


# ---------------------------------------------------------------------------
# 最新唤醒失败时不限次数重试
# ---------------------------------------------------------------------------


@pytest.fixture
def retry_cap(monkeypatch):
    """Dynamic Config 的替身：默认什么都没配；测试可以往里放值。"""
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


async def test_the_latest_wake_is_retried_without_limit(volume, scheduled, retry_cap):
    latest = await wake.set_next_wake(now_cst(), "到点了。")

    cap = await wake.retry_latest_wake_without_limit(_self(latest.message_id))

    assert cap == timedelta(minutes=wake.DEFAULT_WAKE_RETRY_CAP_MINUTES)


async def test_the_retry_cap_comes_from_dynamic_config(volume, scheduled, retry_cap):
    retry_cap[wake.WAKE_RETRY_CAP_MINUTES_KEY] = 5
    latest = await wake.set_next_wake(now_cst(), "到点了。")

    assert await wake.retry_latest_wake_without_limit(_self(latest.message_id)) == timedelta(
        minutes=5
    )


@pytest.mark.parametrize("configured", [0, -3])
async def test_a_cap_that_is_not_positive_falls_back_to_the_default(
    volume, scheduled, retry_cap, configured
):
    retry_cap[wake.WAKE_RETRY_CAP_MINUTES_KEY] = configured
    latest = await wake.set_next_wake(now_cst(), "到点了。")

    assert await wake.retry_latest_wake_without_limit(_self(latest.message_id)) == timedelta(
        minutes=wake.DEFAULT_WAKE_RETRY_CAP_MINUTES
    )


async def test_other_messages_keep_the_limited_retries(volume, scheduled, retry_cap):
    """别人的消息、被取代的旧唤醒、机制的告知：照常有限次重试，然后进死信。"""
    replaced = await wake.set_next_wake(now_cst(), "被取代的。")
    await wake.set_next_wake(now_cst() + timedelta(hours=1), "后来定的。")
    from_operator = new_message(
        sender="operator", recipient="world", body="有人敲门。", kind=Kind.MESSAGE
    )
    notice = new_message(
        sender="world", recipient="world", body="没有送达。", kind=Kind.NOT_DELIVERED
    )

    for message in (from_operator, _self(replaced.message_id), notice):
        assert await wake.retry_latest_wake_without_limit(message) is None
