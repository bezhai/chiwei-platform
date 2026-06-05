"""life 工具循环的工具定义测试 (Task 3 + 阶段 1A act 范式).

life 不再填一张 LifeDecision 表，而是在 ReAct 循环里连续调工具行动。两件工具：

  * ``update_life_state`` —— 更新她此刻在干嘛 / 什么情绪 / 活动类型；可调 0 次或
    多次，多次以最后一次为准（spec 决策 2）。落到 ``save_life_state``。
  * ``act`` —— 她自主做一件影响外部世界的事（自然语言 ``description``，如
    "我去厨房做饭"）。act 是"她做了"、不是申请裁决：直接汇给 world 让它推演
    客观结果。act_id 由本轮 (lane + persona + 读到的 event_ids) 派生 —— 整轮重放
    幂等。落到 ``perform_act`` handler。

工具是 per-round 闭包：``build_life_tools(lane, persona_id, act_id, observed_at)``
把这一轮的绑定（她是谁、哪个泳道、本轮 act_id、观测时刻）capture 进去，模型
只看见业务参数（current_state / response_mood / activity_type / description），看不到
lane / act_id 这些机制绑定。
"""

from __future__ import annotations

import pytest

import app.nodes.life_tools as lt
from app.agent.tooling import Tool


@pytest.fixture
def stub_handlers(monkeypatch):
    """把工具底下的 durable handler 换成可观测 fake。"""
    state: dict = {"saved": [], "acts": []}

    async def fake_save_life_state(**kwargs):
        state["saved"].append(kwargs)

    async def fake_perform_act(**kwargs):
        state["acts"].append(kwargs)

    monkeypatch.setattr(lt, "save_life_state", fake_save_life_state)
    monkeypatch.setattr(lt, "perform_act", fake_perform_act)
    return state


def _tools_by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {t.name: t for t in tools}


def test_build_life_tools_returns_the_two_tools():
    """工具集就是 update_life_state + act 两件，且都是 neutral Tool。"""
    tools = lt.build_life_tools(
        lane="coe-t3",
        persona_id="akao",
        act_id="a-1",
        observed_at="2026-06-03T12:30:00+00:00",
    )
    by_name = _tools_by_name(tools)
    assert set(by_name) == {"update_life_state", "act"}
    for t in tools:
        assert isinstance(t, Tool)


def test_act_tool_name_is_act():
    """对模型暴露的 Tool.name / definition.name 都是 "act"（不是函数名 act_tool）。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    act = tools["act"]
    assert act.name == "act"
    assert act.definition.name == "act"


def test_tool_schema_hides_mechanism_bindings():
    """模型只看见业务参数，看不见 lane / persona_id / act_id / observed_at。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    update_props = set(tools["update_life_state"].definition.parameters["properties"])
    assert update_props == {"current_state", "response_mood", "activity_type"}

    act_props = set(tools["act"].definition.parameters["properties"])
    assert act_props == {"description"}


@pytest.mark.asyncio
async def test_update_life_state_tool_calls_handler(stub_handlers):
    """update_life_state 调一次 → save_life_state 收到这一轮的绑定 + 模型给的字段。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["update_life_state"].invoke(
        {
            "current_state": "起身去厨房",
            "response_mood": "迷糊",
            "activity_type": "move",
        }
    )
    assert stub_handlers["saved"] == [
        {
            "lane": "coe-t3",
            "persona_id": "akao",
            "current_state": "起身去厨房",
            "response_mood": "迷糊",
            "activity_type": "move",
            "observed_at": "2026-06-03T12:30:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_update_life_state_multiple_calls_all_recorded(stub_handlers):
    """update 可调多次（每次 append 一版）—— 收口读最新即"最后一次为准"。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["update_life_state"].invoke(
        {"current_state": "想了想", "response_mood": "平静", "activity_type": "idle"}
    )
    await tools["update_life_state"].invoke(
        {"current_state": "决定去厨房", "response_mood": "期待", "activity_type": "move"}
    )
    assert [s["current_state"] for s in stub_handlers["saved"]] == [
        "想了想",
        "决定去厨房",
    ]


@pytest.mark.asyncio
async def test_act_tool_uses_round_act_id(stub_handlers):
    """act 用本轮派生的 act_id（整轮重放幂等），模型只给 description。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["act"].invoke({"description": "我去厨房煮咖啡"})
    assert stub_handlers["acts"] == [
        {
            "lane": "coe-t3",
            "act_id": "derived-round-id",
            "persona_id": "akao",
            "description": "我去厨房煮咖啡",
            "occurred_at": "2026-06-03T12:30:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_act_second_call_does_not_emit_and_feeds_back(stub_handlers, caplog):
    """一轮里第二次调 act 不再落 handler（一轮只允许做一件事生效）。

    act_id 由本轮 event_ids 派生 —— 同一轮里 N 次 act 会共用同一个 act_id，第二个
    及以后会被 durable 去重层按 act_id 静默吞掉、动作无声丢失。第一刀：本轮已做过
    一件事后，第二次调用不再落 handler，而是 log warning + 返回一句提示喂回模型
    （绝不静默吞）。
    """
    import logging

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )

    with caplog.at_level(logging.WARNING):
        first = await tools["act"].invoke({"description": "我去厨房煮咖啡"})
        second = await tools["act"].invoke({"description": "顺便给千凪带一杯"})

    # 只有第一次真正落到 handler（只 emit 一个动作），第二次不落
    assert len(stub_handlers["acts"]) == 1
    assert stub_handlers["acts"][0]["description"] == "我去厨房煮咖啡"

    # 第二次返回一句提示喂回模型，不是 None、不是静默吞
    assert isinstance(second, str)
    assert second != first
    assert second  # 非空提示

    # 第二次被丢要 log warning（不静默）
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_act_description_guides_toward_low_action():
    """act 措辞软引导降频（spec 决策 5 内容判断那层）。

    多数时候她只是经历这一刻（更新状态就够），只有做的事会在自己之外留下痕迹、被
    够得着的人感知到时才 act。这是软内容引导（赤尾宪法：不加 if 强制），所以只能断言指令文本已改、不能
    断言行为。也验证旧"申请 / 裁决"语义已不在文案里——act 是"你做了"不是"你请求"。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["act"].definition.description
    # 新判据软引导：act 是"会在你之外的世界留下痕迹、被够得着的人感知到"的事
    assert "留下痕迹" in desc
    assert ("经历这一刻" in desc) or ("多数时候" in desc)
    # act 是"你做了"不是"申请 / 等批准"——旧裁决语义不该残留
    assert "裁决" not in desc
    assert "批准" not in desc


@pytest.mark.asyncio
async def test_tool_failure_returns_outcome_not_raise(monkeypatch):
    """单个工具自身抛错被吞掉、喂回模型让它自纠，不炸整轮（spec 决策 3）。"""

    async def boom(**kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(lt, "save_life_state", boom)

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            act_id="a-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["update_life_state"].invoke(
        {"current_state": "x", "response_mood": "y", "activity_type": "z"}
    )
    # @tool_error 把失败变成结构化 outcome dict，不抛
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error"
