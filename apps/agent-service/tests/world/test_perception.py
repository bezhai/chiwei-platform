"""感知判断：主 agent 报告一个变化 → 一次感知判断 → 每个判断会察觉的人一条告知；以及告知居民
只有这一条路。

模型换成替身（主 agent 在一轮的 context 里真调 ``report_change``，感知判断的替身在它那次调用的
context 里真调 ``someone_notices``），通信机制的 ``send`` 换成替身。
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import httpx
import pytest
from openai import InternalServerError

import app.world
from app.capabilities._errors import CapabilityTimeout
from app.infra.cst_time import now_cst
from app.messaging.message import Kind, new_message
from app.world import main_agent, perception, wake
from app.world.actions import ACTIONS, report_change
from app.world.agents import when
from app.world.sources import query_tools

from .conftest import LANE, ScriptedAgent, sets_wake, tools_built_for


def judges(*judgments: tuple, said: str = "判断完了。"):
    """感知判断替身：按顺序判断这些人会察觉到什么。每条是 (谁, 察觉到什么)，或者再加一项要不要
    现在就让他注意到（不写就是要）。"""

    async def plan(_input):
        for who, what, *right_away in judgments:
            await perception.someone_notices.invoke(
                {"who": who, "what": what, "right_away": right_away[0] if right_away else True}
            )
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
    await world.deliver(
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
            await perception.someone_notices.invoke(
                {"who": "world", "what": "x", "right_away": True}
            ),
            await perception.someone_notices.invoke(
                {"who": "has space", "what": "x", "right_away": True}
            ),
            await perception.someone_notices.invoke(
                {"who": "ayana", "what": "  ", "right_away": True}
            ),
        ]
        return "好。"

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(plan)

    await _a_round(world, reports("下雪了。"))

    assert world.sent == []
    assert all(a.get("kind") == "invalid_args" for a in plan.answers)


async def test_a_body_messaging_would_refuse_goes_back_to_perception_and_is_never_kept(
    world, volume
):
    """通信机制存不下的正文（NUL、单独的代理码位）在判断那一刻就退回给感知判断；记下来、
    发出去的只有收得下的那条。"""
    kept_when_sending: list[str] = []
    world.before_send = lambda _r: kept_when_sending.append(
        (volume / LANE / "unfinished.json").read_text(encoding="utf-8")
    )

    async def plan(_input):
        plan.answers = [
            await perception.someone_notices.invoke(
                {"who": "akao", "what": "门响了\x00一声。", "right_away": True}
            ),
            await perception.someone_notices.invoke(
                {"who": "chinagi", "what": "门响了\ud800一声。", "right_away": True}
            ),
        ]
        await perception.someone_notices.invoke(
            {"who": "ayana", "what": "门响了一声。", "right_away": True}
        )
        return "好。"

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(plan)

    await _a_round(world, reports("门被风吹得响了一声。"))

    assert [a.get("kind") for a in plan.answers] == ["invalid_args", "invalid_args"]
    assert [(s["recipient"], s["body"]) for s in world.sent] == [("ayana", "门响了一声。")]
    [kept] = kept_when_sending
    assert "ayana" in kept and "akao" not in kept and "chinagi" not in kept


async def test_a_change_messaging_could_not_carry_is_handed_back_before_anyone_judges_it(
    world, volume
):
    """变化原文要记进 unfinished 给下一轮看，存不下的字在报告那一刻就退回给主 agent。"""
    judge = judges(("ayana", "x"))
    world.agents[perception.PERCEPTION.prompt_id] = judge

    [result] = await _a_round(world, reports("门响了\x00一声。"))

    assert "没有报告" in result
    assert judge.inputs == [] and world.sent == []
    assert not (volume / LANE / "unfinished.json").exists()


# ---------------------------------------------------------------------------
# 要不要现在就让他注意到：每条告知由感知判断自己说，代码不替它定
# ---------------------------------------------------------------------------


async def test_each_notice_wakes_its_recipient_or_not_as_perception_judged(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(
        ("ayana", "楼下有人喊你的名字。", True), ("akao", "窗外的雨小了一点。", False)
    )

    await _a_round(world, reports("楼下有人在喊，雨也小了。"))

    assert [s["recipient"] for s in world.sent] == ["ayana", "akao"]
    assert world.sent_wakes == [True, False]


async def test_judging_the_same_person_again_also_replaces_whether_to_wake_them(world):
    world.agents[perception.PERCEPTION.prompt_id] = judges(
        ("ayana", "先这么想。", True), ("ayana", "改成这样。", False)
    )

    await _a_round(world, reports("下雨了。"))

    assert [(s["body"], wakes) for s, wakes in zip(world.sent, world.sent_wakes, strict=True)] == [
        ("改成这样。", False)
    ]


def test_perception_has_to_say_whether_to_wake_each_person():
    """这一项必填、没有默认值：每条告知都由模型自己判断，不由代码替它补一个。"""
    parameters = perception.someone_notices.definition.parameters
    assert "right_away" in parameters["required"]
    assert parameters["properties"]["right_away"]["type"] == "boolean"
    assert "default" not in parameters["properties"]["right_away"]


def test_the_model_is_told_what_not_waking_does():
    """false 不是"不告诉他"：这段话照样交给他，只是不为它打断他。模型据此判断，说明里要写清楚。"""
    described = perception.someone_notices.definition.parameters["properties"]["right_away"]
    assert described["description"] == (
        "要不要现在就让他注意到。true：现在就打断他，让他注意到这件事；"
        "false：不为这件事打断他，这段话照样会交给他，他过一会儿自己会看到"
    )


async def test_a_judgment_that_leaves_out_or_garbles_whether_to_wake_is_refused(world):
    async def plan(_input):
        plan.answers = [
            await perception.someone_notices.invoke({"who": "ayana", "what": "下雨了。"}),
            await perception.someone_notices.invoke(
                {"who": "akao", "what": "下雨了。", "right_away": "false"}
            ),
        ]
        return "好。"

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(plan)

    await _a_round(world, reports("下雨了。"))

    assert [a.get("kind") for a in plan.answers] == ["tool_error", "invalid_args"]
    assert world.sent == []


# ---------------------------------------------------------------------------
# 感知判断知道这一轮是被谁的什么消息叫醒的：做事的人已经知道自己做了什么
# ---------------------------------------------------------------------------


async def test_perception_sees_who_sent_the_message_that_woke_this_round_and_what_it_said(
    world,
):
    judge = judges()
    world.agents[perception.PERCEPTION.prompt_id] = judge
    world.runner.plan = reports("窗关上之后，屋里的雨声小了。")
    trigger = new_message(
        sender="赤尾", recipient="world", body="我起身把窗关上了。", kind=Kind.MESSAGE
    )

    await world.deliver(trigger)

    [seen] = judge.inputs
    assert seen.split("\n")[1:] == [
        "【叫醒世界的消息】这一轮世界收到 1 条消息，按到达的先后：",
        f"（1）赤尾 发来（{when(trigger.time)}）：",
        "我起身把窗关上了。",
        "【世界里发生的变化】",
        "窗关上之后，屋里的雨声小了。",
    ]


async def test_perception_is_told_when_world_woke_on_its_own(world):
    """world 给自己排的醒来，正文是它当时给自己留的话，用的是"你"。感知判断那边的"你"是它自己，
    所以要说清楚这段话是谁写给谁的。正文用 world 真的排出去的那一条，不另编。"""
    judge = judges()
    world.agents[perception.PERCEPTION.prompt_id] = judge
    world.runner.plan = sets_wake(reason="看看傍晚的街上。")
    await world.deliver(
        new_message(sender="operator", recipient="world", body="x", kind=Kind.MESSAGE)
    )
    [scheduled] = world.scheduled
    own_wake = new_message(
        sender=scheduled["sender"],
        recipient=scheduled["recipient"],
        body=scheduled["body"],
        kind=Kind.MESSAGE,
        time=scheduled["at"],
        message_id=scheduled["message_id"],
    )
    assert own_wake.body.startswith("你在 ") and own_wake.body.endswith("看看傍晚的街上。"), (
        "前提没造出来：要用 world 排出去的那段原话"
    )
    world.runner.plan = reports("傍晚了，街灯亮了。")

    await world.deliver(own_wake)

    [seen] = judge.inputs
    assert seen.split("\n")[1:4] == [
        "【叫醒世界的消息】这一轮世界收到 1 条消息，按到达的先后：",
        f"（1）世界自己定的一次醒来（{when(own_wake.time)}），不是谁发来的。"
        f"下面是世界当时给自己留的话，话里的\"你\"指世界自己：",
        own_wake.body,
    ]


async def test_perception_sees_every_message_of_the_round_with_sender_and_time(world, volume):
    """一轮带着几条消息时，感知判断看到的是这一轮的全部：每条谁发的、什么时候、原文，按到达的先后。"""
    judge = judges()
    world.agents[perception.PERCEPTION.prompt_id] = judge
    world.runner.plan = reports("窗关上之后，屋里的雨声小了。")
    current = await wake.set_next_wake(now_cst(), "看看雨停了没有。")
    akao = new_message(sender="赤尾", recipient="world", body="我起身把窗关上了。", kind=Kind.MESSAGE)
    own = new_message(
        sender="world",
        recipient="world",
        body="看看雨停了没有。",
        kind=Kind.MESSAGE,
        message_id=current.message_id,
    )
    ayana = new_message(sender="绫奈", recipient="world", body="我在客厅看书。", kind=Kind.MESSAGE)
    bounced = new_message(
        sender="world", recipient="world", body="你发给千凪的消息没有送达。", kind=Kind.NOT_DELIVERED
    )

    await main_agent.run_round([akao, own, ayana, bounced])

    [seen] = judge.inputs
    assert seen.split("\n")[1:] == [
        "【叫醒世界的消息】这一轮世界收到 4 条消息，按到达的先后：",
        f"（1）赤尾 发来（{when(akao.time)}）：",
        "我起身把窗关上了。",
        f"（2）世界自己定的一次醒来（{when(own.time)}），不是谁发来的。"
        f"下面是世界当时给自己留的话，话里的\"你\"指世界自己：",
        "看看雨停了没有。",
        f"（3）绫奈 发来（{when(ayana.time)}）：",
        "我在客厅看书。",
        f"（4）通信机制退回给世界的一条没有送达的消息（{when(bounced.time)}）：",
        "你发给千凪的消息没有送达。",
        "【世界里发生的变化】",
        "窗关上之后，屋里的雨声小了。",
    ]


# ---------------------------------------------------------------------------
# 模型的临时失败（5xx、超时）重试；失败那一次判断过的不带进重试，也不发
# ---------------------------------------------------------------------------


def _server_error() -> Exception:
    """模型那边返回 500：2026-10-06 在 coe-world 上两次丢掉变化的就是它。"""
    request = httpx.Request("POST", "https://model.invalid/v1/chat/completions")
    return InternalServerError(
        "Error code: 500 - The server had an error while processing your request.",
        response=httpx.Response(500, request=request),
        body=None,
    )


def _no_answer() -> Exception:
    return CapabilityTimeout("gpt-5.5 gave no answer within 180s")


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(perception, "RETRY_BASE_SECONDS", 0.0)


@pytest.mark.parametrize("transient", [_server_error, _no_answer])
async def test_a_transient_model_failure_is_retried_and_only_the_retrys_judgments_are_sent(
    world, no_backoff, transient
):
    """失败的那一次已经判断过 akao 和 ayana；重试只判断了 ayana。发出去的只有重试的那一条，
    akao 不会因为失败那一次的判断收到告知，ayana 也只收到一条。"""
    attempts: list[str] = []

    async def plan(perception_input):
        attempts.append(perception_input)
        if len(attempts) == 1:
            await perception.someone_notices.invoke(
                {"who": "akao", "what": "失败那一次写的。", "right_away": True}
            )
            await perception.someone_notices.invoke(
                {"who": "ayana", "what": "失败那一次写的。", "right_away": True}
            )
            raise transient()
        await perception.someone_notices.invoke(
            {"who": "ayana", "what": "你听见楼下的门响了一声。", "right_away": False}
        )
        return "判断完了。"

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(plan)
    world.open_inboxes = {"ayana"}

    [result] = await _a_round(world, reports("楼下的门被风吹得响了一声。"))

    assert len(attempts) == 2
    assert attempts[1] == attempts[0], "重试拿到的是同一份输入"
    assert [(s["recipient"], s["body"]) for s in world.sent] == [
        ("ayana", "你听见楼下的门响了一声。")
    ]
    assert world.sent_wakes == [False]
    assert "ayana" in result and "送达了" in result and "akao" not in result


async def test_when_every_attempt_fails_nothing_is_sent_and_the_failure_shows(
    world, no_backoff, caplog
):
    attempts: list[str] = []

    async def plan(perception_input):
        attempts.append(perception_input)
        await perception.someone_notices.invoke(
            {"who": "ayana", "what": "没做成的那一次写的。", "right_away": True}
        )
        raise _server_error()

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(plan)

    with caplog.at_level(logging.WARNING):
        [result] = await _a_round(world, reports("楼下的门被风吹得响了一声。"))

    assert len(attempts) == perception.PERCEPTION_ATTEMPTS > 1
    assert world.sent == []
    # 主 agent 照旧拿到"没有报告出去"（用完之后的处理不变），日志里点明试了几次、最后为什么失败。
    assert "没有报告出去" in result and "InternalServerError" in result
    [gave_up] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert f"{perception.PERCEPTION_ATTEMPTS} attempts" in gave_up.getMessage()
    retried = [r for r in caplog.records if "retrying" in r.getMessage()]
    assert len(retried) == perception.PERCEPTION_ATTEMPTS - 1


async def test_a_failure_that_is_not_transient_is_not_retried(world, no_backoff):
    attempts: list[str] = []

    async def plan(perception_input):
        attempts.append(perception_input)
        raise ValueError("模型交回来的东西解不开")

    world.agents[perception.PERCEPTION.prompt_id] = ScriptedAgent(plan)

    [result] = await _a_round(world, reports("下雨了。"))

    assert len(attempts) == 1
    assert "没有报告出去" in result and world.sent == []


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
