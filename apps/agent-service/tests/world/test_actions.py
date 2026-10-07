"""主 agent 才有的动作：写记录、定下次醒来的时刻。

动作在一轮的 ambient context 里跑（:class:`app.world.actions.RoundScope`，加上记录来源记
读过什么的那个字典），这里直接在 ``agent_context`` 里调它们，不起模型。
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.agent.context import AgentContext
from app.agent.runtime_context import agent_context
from app.infra.cst_time import CST, now_cst
from app.messaging.message import Kind, new_message
from app.world import actions, records
from app.world.actions import ROUND_SCOPE, RoundScope
from app.world.sources.records import RECORDS_READ, read_record


@pytest.fixture
def scope(volume) -> RoundScope:
    s = RoundScope(
        messages=(
            new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE),
        )
    )
    ctx = AgentContext(features={ROUND_SCOPE: s, RECORDS_READ: {}})
    with agent_context(ctx):
        yield s


async def _call(t, **arguments):
    return await t.invoke(arguments)


# ---------------------------------------------------------------------------
# 写记录
# ---------------------------------------------------------------------------


async def test_a_new_record_can_be_written_without_reading_first(scope):
    result = await _call(actions.write_record, path="人/乙.md", text="一个新来的人。")

    assert "写好了" in result
    assert records.read("人/乙.md").text == "一个新来的人。"


async def test_an_existing_record_is_rewritten_only_after_reading_it_this_round(scope):
    records.write("地方/甲.md", "旧的样子。", expected=None)

    refused = await _call(actions.write_record, path="地方/甲.md", text="没读就改。")
    assert "先读" in str(refused)
    assert records.read("地方/甲.md").text == "旧的样子。"

    shown = await _call(read_record, path="地方/甲.md")
    assert "旧的样子。" in shown
    await _call(actions.write_record, path="地方/甲.md", text="新的样子。")
    assert records.read("地方/甲.md").text == "新的样子。"

    # 自己刚写过的，同一轮里接着改不用再读一遍。
    await _call(actions.write_record, path="地方/甲.md", text="又改了一次。")
    assert records.read("地方/甲.md").text == "又改了一次。"


async def test_a_record_changed_by_someone_else_after_reading_is_not_overwritten(scope):
    first = records.write("地方/甲.md", "它读到的。", expected=None)
    await _call(read_record, path="地方/甲.md")
    records.write("地方/甲.md", "人工改过的。", expected=first.fingerprint)

    refused = await _call(actions.write_record, path="地方/甲.md", text="拿旧的改。")

    assert "重新读" in str(refused)
    assert records.read("地方/甲.md").text == "人工改过的。"


async def test_a_bad_path_is_reported_not_raised(scope):
    result = await _call(actions.write_record, path="../越界.md", text="x")

    assert "记录路径" in str(result) or "走出" in str(result)


# ---------------------------------------------------------------------------
# 定下次醒来的时刻
# ---------------------------------------------------------------------------


async def test_wake_me_at_sets_the_rounds_next_wake(scope):
    at = (now_cst() + timedelta(hours=3)).replace(microsecond=0)

    result = await _call(actions.wake_me_at, at=at.isoformat(), reason="等雨停。")

    assert scope.next_wake == actions.WakeChoice(at=at, reason="等雨停。")
    assert "定好了" in result


async def test_the_last_wake_set_in_a_round_wins(scope):
    first = now_cst() + timedelta(hours=1)
    second = now_cst() + timedelta(hours=2)

    await _call(actions.wake_me_at, at=first.isoformat(), reason="先这样。")
    await _call(actions.wake_me_at, at=second.isoformat(), reason="改主意了。")

    assert scope.next_wake.reason == "改主意了。"


async def test_a_time_without_an_offset_is_read_as_utc_plus_eight(scope):
    local = (now_cst() + timedelta(hours=1)).replace(tzinfo=None, microsecond=0)

    await _call(actions.wake_me_at, at=local.isoformat(), reason="一小时后。")

    assert scope.next_wake.at == local.replace(tzinfo=CST)


def test_the_model_is_told_which_reason_it_sees_when_it_keeps_the_same_time():
    """时刻跟原来定的下次醒来相同时不另排，到点摆出来的是原来那次写的理由
    （:func:`app.world.wake.set_next_wake`）。说明里不能许诺"这一次写的会原样摆出来"。"""
    described = actions.wake_me_at.definition.parameters["properties"]["reason"]
    assert described["description"] == (
        "为什么定这个时刻。到时候这句话会原样摆在你眼前；时刻跟你原来定的下次醒来相同时，"
        "不另排，到时候摆出来的是原来那次写的理由"
    )


@pytest.mark.parametrize(
    "offset_minutes,reason",
    [
        (None, "理由"),  # 不是时间
        (-1, "已经过去的时刻"),
        (60, "   "),  # 没写理由
    ],
)
async def test_an_unusable_wake_is_refused_and_nothing_is_set(scope, offset_minutes, reason):
    at = (
        "不是时间"
        if offset_minutes is None
        else (now_cst() + timedelta(minutes=offset_minutes)).isoformat()
    )
    result = await _call(actions.wake_me_at, at=at, reason=reason)

    assert scope.next_wake is None
    assert isinstance(result, dict) and result.get("kind") == "invalid_args"
