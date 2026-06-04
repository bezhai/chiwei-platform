"""life 工具循环的工具定义测试 (Task 3).

life 不再填一张 LifeDecision 表，而是在 ReAct 循环里连续调工具行动。两件工具：

  * ``update_life_state`` —— 更新她此刻在干嘛 / 什么情绪 / 活动类型；可调 0 次或
    多次，多次以最后一次为准（spec 决策 2）。落到 ``save_life_state``。
  * ``raise_intent`` —— 觉得想做点什么就起个意图回灌 world。intent_id 由本轮
    (lane + persona + 读到的 event_ids) 派生 —— 整轮重放幂等。落到
    ``raise_intent`` handler。

工具是 per-round 闭包：``build_life_tools(lane, persona_id, intent_id, observed_at)``
把这一轮的绑定（她是谁、哪个泳道、本轮 intent_id、观测时刻）capture 进去，模型
只看见业务参数（current_state / response_mood / activity_type / summary），看不到
lane / intent_id 这些机制绑定。
"""

from __future__ import annotations

import pytest

import app.nodes.life_tools as lt
from app.agent.tooling import Tool


@pytest.fixture
def stub_handlers(monkeypatch):
    """把工具底下的 durable handler 换成可观测 fake。"""
    state: dict = {"saved": [], "intents": []}

    async def fake_save_life_state(**kwargs):
        state["saved"].append(kwargs)

    async def fake_raise_intent(**kwargs):
        state["intents"].append(kwargs)

    monkeypatch.setattr(lt, "save_life_state", fake_save_life_state)
    monkeypatch.setattr(lt, "raise_intent", fake_raise_intent)
    return state


def _tools_by_name(tools: list[Tool]) -> dict[str, Tool]:
    return {t.name: t for t in tools}


def test_build_life_tools_returns_the_two_tools():
    """工具集就是 update_life_state + raise_intent 两件，且都是 neutral Tool。"""
    tools = lt.build_life_tools(
        lane="coe-t3",
        persona_id="akao",
        intent_id="i-1",
        observed_at="2026-06-03T12:30:00+00:00",
    )
    by_name = _tools_by_name(tools)
    assert set(by_name) == {"update_life_state", "raise_intent"}
    for t in tools:
        assert isinstance(t, Tool)


def test_tool_schema_hides_mechanism_bindings():
    """模型只看见业务参数，看不见 lane / persona_id / intent_id / observed_at。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            intent_id="i-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    update_props = set(tools["update_life_state"].definition.parameters["properties"])
    assert update_props == {"current_state", "response_mood", "activity_type"}

    intent_props = set(tools["raise_intent"].definition.parameters["properties"])
    assert intent_props == {"summary"}


@pytest.mark.asyncio
async def test_update_life_state_tool_calls_handler(stub_handlers):
    """update_life_state 调一次 → save_life_state 收到这一轮的绑定 + 模型给的字段。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            intent_id="i-1",
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
            intent_id="i-1",
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
async def test_raise_intent_tool_uses_round_intent_id(stub_handlers):
    """raise_intent 用本轮派生的 intent_id（整轮重放幂等），模型只给 summary。"""
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            intent_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    await tools["raise_intent"].invoke({"summary": "去厨房煮咖啡"})
    assert stub_handlers["intents"] == [
        {
            "lane": "coe-t3",
            "intent_id": "derived-round-id",
            "persona_id": "akao",
            "summary": "去厨房煮咖啡",
            "occurred_at": "2026-06-03T12:30:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_raise_intent_second_call_does_not_emit_and_feeds_back(
    stub_handlers, caplog
):
    """一轮里第二次调 raise_intent 不再落 handler（一轮只允许一个意图生效）。

    intent_id 由本轮 event_ids 派生 —— 同一轮里 N 次 raise_intent 会共用同一个
    intent_id，第二个及以后会被 durable 去重层按 intent_id 静默吞掉、意图无声丢失。
    第一刀：本轮已起过意图后，第二次调用不再落 handler，而是 log warning + 返回一句
    提示喂回模型（绝不静默吞）。
    """
    import logging

    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            intent_id="derived-round-id",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )

    with caplog.at_level(logging.WARNING):
        first = await tools["raise_intent"].invoke({"summary": "去厨房煮咖啡"})
        second = await tools["raise_intent"].invoke({"summary": "顺便给千凪带一杯"})

    # 只有第一次真正落到 handler（只 emit 一个意图），第二次不落
    assert len(stub_handlers["intents"]) == 1
    assert stub_handlers["intents"][0]["summary"] == "去厨房煮咖啡"

    # 第二次返回一句提示喂回模型，不是 None、不是静默吞
    assert isinstance(second, str)
    assert second != first
    assert second  # 非空提示

    # 第二次被丢要 log warning（不静默）
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


def test_raise_intent_description_guides_toward_low_intent():
    """raise_intent 措辞软引导降频（spec 决策 5 内容判断那层）。

    旧措辞"回应刚才的动静就调"是高频自激起点（每个 event 都触发一次起意图）。改成
    引导她"多数时候只是经历这一刻（更新状态就够），只有真要改变处境才起意图"。这是
    软内容引导（赤尾宪法：不加 if 强制），所以只能断言指令文本已改、不能断言行为。
    """
    tools = _tools_by_name(
        lt.build_life_tools(
            lane="coe-t3",
            persona_id="akao",
            intent_id="i-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    desc = tools["raise_intent"].definition.description
    # 旧的高频自激措辞已移除（"回应刚才的动静"这句是每个 event 都起意图的起点）
    assert "回应刚才的动静" not in desc
    # 新措辞软引导：多数时候只是经历这一刻 + 只有真改变处境才起意图
    assert "改变处境" in desc
    assert ("经历这一刻" in desc) or ("多数时候" in desc)


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
            intent_id="i-1",
            observed_at="2026-06-03T12:30:00+00:00",
        )
    )
    out = await tools["update_life_state"].invoke(
        {"current_state": "x", "response_mood": "y", "activity_type": "z"}
    )
    # @tool_error 把失败变成结构化 outcome dict，不抛
    assert isinstance(out, dict)
    assert out["kind"] == "tool_error"
