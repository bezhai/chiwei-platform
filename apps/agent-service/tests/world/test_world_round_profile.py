"""world 主 agent 的一轮由 world 和三个知识来源插件登记的东西组成，按清单 ``world`` 排的位置跑
（:func:`app.agent.round.run_round`）：一轮开始前补发没跑完的一轮的告知；状态段是记录目录（只进
清理标记）；三个输入段用一个换行拼成它眼前的那条消息；检查它定了下次醒来；没有落地事务；之后存
上下文、清空没跑完的一轮、定下次醒来，三步任何一步失败这一轮都算没跑完。

模型、上下文存储、成本、定时发送都是替身（``world`` fixture）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.agent.continuity import TranscriptConflict
from app.agent.contracts import CostSpec, FailRound, Switches
from app.agent.round import current_round
from app.infra.cst_time import CST, now_cst
from app.messaging.message import Kind, new_message
from app.world import main_agent, records, wake
from app.world.agents import when
from app.world.perception import unfinished
from app.world.services import Notice

from .conftest import (
    LANE,
    run_round_taking,
    self_message,
    sets_nothing,
    sets_wake,
    tools_built_for,
)

NOW = datetime(2026, 7, 25, 14, 3, tzinfo=CST)


def _round_input(world_handle) -> str:
    return world_handle.runner.runs[-1][-1].content


async def test_the_world_profile_places_what_world_and_its_three_sources_register(
    world,
):
    p = world.profile

    assert (p.profile.agent, p.profile.sub_agents) == (
        "world_round",
        ("world_perception", "world_npc"),
    )
    assert [(t.name, t.plugin, t.value.trim) for t in p.tools] == [
        ("list_records", "records", "material"),
        ("read_record", "records", "material"),
        ("check_current_weather", "reality", "material"),
        ("check_hourly_forecast", "reality", "material"),
        ("check_daily_forecast", "reality", "material"),
        ("search_web", "reality", "material"),
        ("list_senders", "told", "material"),
        ("read_messages_from", "told", "material"),
        ("write_record", "record_keeping", "kept"),
        ("wake_me_at", "world", "kept"),
        ("report_change", "perception", "kept"),
        ("let_npc_appear", "npc", "kept"),
    ]
    assert not any(t.value.ends_round for t in p.tools)
    assert sorted(p.segments) == sorted(
        ["world_npc", "world_perception", *main_agent.ROUND_SEGMENTS]
    )
    assert [s.name for s in p.round_start] == ["unfinished.resend"]
    assert [(s.name, s.plugin) for s in p.state] == [("records.listing", "record_keeping")]
    assert p.segments["world_round/write-record"].plugin == "record_keeping"
    assert p.segments["world_round/reality"].plugin == "reality"
    assert {
        name: p.segments[f"world_round/{name}"].plugin
        for name in ("report-change", "unfinished")
    } == {"report-change": "perception", "unfinished": "perception"}
    assert p.segments["world_perception"].plugin == "perception"
    assert p.segments["world_npc"].plugin == p.segments["world_round/npc"].plugin == "npc"
    assert [(s.name, s.plugin) for s in p.round_start] == [("unfinished.resend", "perception")]
    assert [(s.name, s.plugin) for s in p.input] == [
        ("wake.woken_by", "world"),
        ("wake.planned", "world"),
        ("unfinished.happenings", "perception"),
    ]
    assert [(s.name, s.plugin) for s in p.after_commit] == [
        ("history", "world"),
        ("unfinished.clear", "perception"),
        ("wake.next", "world"),
    ]
    assert [s.name for s in p.input] == [
        "wake.woken_by",
        "wake.planned",
        "unfinished.happenings",
    ]
    assert p.profile.join == "\n"
    assert [s.name for s in p.checks] == ["wake.chosen"]
    assert p.commit == ()
    assert [(s.name, s.value.on_failure) for s in p.after_commit] == [
        ("history", FailRound()),
        ("unfinished.clear", FailRound()),
        ("wake.next", FailRound()),
    ]
    assert p.round_log is None
    assert p.profile.history.key.format(lane=LANE) == f"world:{LANE}"
    assert p.profile.history.policy is main_agent.TRIM_POLICY
    assert p.profile.cost == CostSpec(actor="world", round_id="{trace}:{round_id}")
    assert p.profile.switches == Switches(
        "world_sources", ("records", "reality", "told")
    )


async def test_switching_records_off_leaves_writing_the_listing_and_its_segment_in_the_round(
    world, monkeypatch
):
    """关掉 ``records`` 只拿掉两个查询工具：写记录、记录目录（状态段）和写记录那一段 prompt 都在
    ``record_keeping``，照旧在这一轮里。"""
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(
        dynamic_config,
        "get",
        lambda k, default="": "reality,told" if k == "world_sources" else default,
    )
    records.write("地方/甲.md", "一二三。", expected=None)
    world.history = []
    segments: list[str] = []

    async def plan():
        segments.extend(p for p, _, _ in current_round().prompts.versions("world_round"))
        return await sets_wake()()

    world.runner.plan = plan
    await run_round_taking(world, [self_message("m-1")], now=NOW)

    tools = tools_built_for(world, main_agent.ROUND)
    assert "write_record" in tools
    assert not {"list_records", "read_record"} & set(tools)
    assert "world_round/write-record" in segments
    assert "你的记录（1 份）" in world.runner.runs[-1][0].content


async def test_switching_reality_off_leaves_its_segment_out_of_the_rounds_prompt(
    world, monkeypatch
):
    """关掉 ``reality``，它的查询工具和它在主 agent 的 prompt 里的那一段都不进这一轮（决定 8）。"""
    from inner_shared.dynamic_config import dynamic_config

    monkeypatch.setattr(
        dynamic_config,
        "get",
        lambda k, default="": "records,told" if k == "world_sources" else default,
    )
    segments: list[str] = []

    async def plan():
        segments.extend(p for p, _, _ in current_round().prompts.versions("world_round"))
        return await sets_wake()()

    world.runner.plan = plan
    await run_round_taking(world, [self_message("m-1")], now=NOW)

    assert segments == [
        s for s in main_agent.ROUND_SEGMENTS if s != "world_round/reality"
    ]
    assert "check_current_weather" not in tools_built_for(world, main_agent.ROUND)


async def test_the_three_input_sections_joined_by_a_newline_are_the_rounds_input(world):
    planned = await wake.set_next_wake(
        datetime(2026, 7, 25, 18, 0, tzinfo=CST), "傍晚再看。"
    )
    unfinished.note(
        "你报告了一个变化：起风了。",
        [Notice(who="赤尾", what="起风了。", wakes_recipient=False, message_id="n-1")],
    )
    [happening] = unfinished.read()
    world.open_inboxes.add("赤尾")
    resident = new_message(
        sender="赤尾", recipient="world", body="我把窗关上了。", kind=Kind.MESSAGE
    )
    bounced = new_message(
        sender="world",
        recipient="world",
        body="你发给千凪的消息没有送达。",
        kind=Kind.NOT_DELIVERED,
    )

    await run_round_taking(world, [resident, bounced], now=NOW)

    assert _round_input(world) == "\n".join(
        [
            f"【现在】{when(NOW)}",
            "【叫醒你的】这一轮有 2 条消息，按到达的先后：",
            f"（1）赤尾 发来一条消息（{when(resident.time)}）：",
            "我把窗关上了。",
            f"（2）通信机制告诉你，你的一条消息没有送达（{when(bounced.time)}）：",
            "你发给千凪的消息没有送达。",
            f"【你原来定的下次醒来】{when(planned.at)}。当时的说明：傍晚再看。",
            "这一轮结束前要重新定一个时刻；还想按原来的来，就再定一次同一个时刻。",
            "【之前没跑完的一轮里已经发生的事】之前有一轮没有正常收尾。那一轮的经过你可能记得，"
            "也可能不记得；不管记不记得，下面这些在那一轮里已经发生了，记录里可能还没写。那一轮"
            "要发的告知，这一轮开始前已经按原样补发了一遍（收到过的人不会再收到一遍），下面是"
            "补发的结果：",
            f"- {when(happening.at)}",
            "你报告了一个变化：起风了。",
            "感知判断之后，告知了这些人：",
            "- 赤尾：「起风了。」——送达了",
        ]
    )
    assert world.sent_ids == ["n-1"]


async def test_a_round_taking_its_own_wake_with_nothing_left_over_is_shown_the_first_section_only(
    world,
):
    current = await wake.set_next_wake(now_cst(), "该看看外面了。")
    own = self_message(current.message_id, "该看看外面了。")

    await run_round_taking(world, [own], now=NOW)

    assert _round_input(world) == "\n".join(
        [
            f"【现在】{when(NOW)}",
            "【叫醒你的】这一轮有 1 条消息，按到达的先后：",
            f"（1）你给自己排的一次醒来（排在 {when(own.time)}）：",
            "该看看外面了。",
        ]
    )


def _from_operator():
    return new_message(
        sender="operator", recipient="world", body="x", kind=Kind.MESSAGE
    )


async def test_a_round_without_a_next_wake_fails_with_the_text_its_last_error_shows(
    world,
):
    unfinished.note("你报告了一个变化：起风了。", [])
    world.runner.plan = sets_nothing()
    message = _from_operator()

    with pytest.raises(main_agent.NoNextWake) as failed:
        await run_round_taking(world, [message], round_id="r-1", now=NOW)

    assert str(failed.value) == (
        f"world round r-1 (taking {message.message_id} (message from operator)) "
        f"ended without setting its next wake"
    )
    assert world.committed == []
    assert [h.what for h in unfinished.read()] == ["你报告了一个变化：起风了。"]
    assert world.scheduled == [] and wake.read_next_wake() is None


def _history_conflicts(world_handle, monkeypatch):
    async def conflict(*_a, **_kw):
        raise TranscriptConflict("别人写过了")

    monkeypatch.setattr("app.agent.round.commit_transcript", conflict)
    return TranscriptConflict


def _clearing_fails(world_handle, monkeypatch):
    def broken():
        raise OSError("unfinished.json 删不掉")

    monkeypatch.setattr(unfinished, "clear", broken)
    return OSError


def _scheduling_fails(world_handle, monkeypatch):
    async def broken(**_kw):
        raise ConnectionError("broker 没有确认")

    monkeypatch.setattr(wake, "send_at", broken)
    return ConnectionError


@pytest.mark.parametrize(
    ("step", "breaks", "then_unfinished"),
    [
        ("history", _history_conflicts, ["你报告了一个变化：起风了。"]),
        ("unfinished.clear", _clearing_fails, ["你报告了一个变化：起风了。"]),
        ("wake.next", _scheduling_fails, []),
    ],
)
async def test_each_step_after_the_model_fails_the_round_when_it_fails(
    world, monkeypatch, step, breaks, then_unfinished
):
    """每一步失败都往外抛，这一轮算没跑完；它后面的步骤不再做（定下次醒来是最后一步，所以三种
    情况下都没有定下新的醒来）。"""
    unfinished.note("你报告了一个变化：起风了。", [])
    error = breaks(world, monkeypatch)

    with pytest.raises(error):
        await run_round_taking(world, [_from_operator()], now=NOW)

    assert len(world.runner.runs) == 1, step
    assert [h.what for h in unfinished.read()] == then_unfinished
    assert wake.read_next_wake() is None


async def test_the_next_wake_is_set_with_the_reason_it_wrote_down_at_the_rounds_time(
    world,
):
    await run_round_taking(world, [_from_operator()], now=NOW)

    chosen = wake.read_next_wake()
    assert chosen.at > now_cst() + timedelta(hours=1)
    assert (
        chosen.reason
        == f"你在 {when(NOW)} 定下这个时刻醒来，当时写下的理由：过一阵再看看。"
    )
