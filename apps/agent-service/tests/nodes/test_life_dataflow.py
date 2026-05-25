"""Phase 4 life_dataflow @node tests."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from app.domain.life_dataflow import (
    GlimpseRequest,
    GlimpseTickRequest,
    LifeStateChanged,
    LifeTickRequest,
    MinuteTick,
    VoiceRequest,
)
from app.runtime.emit import reset_emit_runtime
from app.runtime.placement import clear_bindings
from app.runtime.wire import clear_wiring


@pytest.fixture
def reset_runtime():
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()
    yield
    clear_wiring()
    clear_bindings()
    reset_emit_runtime()


@pytest.fixture
def mock_prod():
    with patch("app.nodes.life_dataflow._is_prod", return_value=True):
        yield


@pytest.fixture
def mock_personas(monkeypatch):
    """B7: ``_persona_dicts`` replaces the old ``_list_persona_ids``
    helper; the wire-level ``fan_out_per`` calls it to fan a template
    Request into per-persona copies."""
    async def _fake_dicts():
        return [{"persona_id": "p1"}, {"persona_id": "p2"}]
    monkeypatch.setattr("app.nodes.life_dataflow._persona_dicts", _fake_dicts)


@pytest.mark.asyncio
async def test_fan_out_life_tick_emits_per_persona(reset_runtime, mock_prod, mock_personas):
    """fan_out_life_tick emits a template Request; the wire's
    ``.fan_out_per(_persona_dicts)`` fans it into per-persona copies."""
    from app.nodes.life_dataflow import _persona_dicts, fan_out_life_tick
    from app.runtime import wire
    from app.runtime.graph import compile_graph
    from app.runtime.node import node

    seen: list[LifeTickRequest] = []

    async def _capture(r: LifeTickRequest) -> None:
        seen.append(r)
    probe = node(_capture)
    wire(LifeTickRequest).fan_out_per(_persona_dicts).to(probe)
    compile_graph()

    await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert {r.persona_id for r in seen} == {"p1", "p2"}


@pytest.mark.asyncio
async def test_fan_out_life_tick_skips_non_prod(reset_runtime, mock_personas, monkeypatch):
    """Non-prod lane → fan_out_xxx @node returns without emit; nothing
    reaches the wire's fan_out_per stage."""
    monkeypatch.setattr("app.nodes.life_dataflow._is_prod", lambda: False)
    from app.nodes.life_dataflow import _persona_dicts, fan_out_life_tick
    from app.runtime import wire
    from app.runtime.graph import compile_graph
    from app.runtime.node import node

    seen: list = []

    async def _capture(r: LifeTickRequest) -> None:
        seen.append(r)
    wire(LifeTickRequest).fan_out_per(_persona_dicts).to(node(_capture))
    compile_graph()

    await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_fan_out_life_tick_swallows_db_error(reset_runtime, mock_prod, monkeypatch, caplog):
    """DB 抖动不冒泡到 source loop — extractor exception is now caught
    by ``emit._dispatch_fan_out`` (was caught by the hand-rolled
    ``_fan_out_per_persona`` try/except before B7)."""
    import logging

    async def _boom():
        raise RuntimeError("db down")
    monkeypatch.setattr("app.nodes.life_dataflow._persona_dicts", _boom)

    from app.nodes.life_dataflow import _persona_dicts, fan_out_life_tick
    from app.runtime import wire
    from app.runtime.graph import compile_graph
    from app.runtime.node import node

    async def _capture(r: LifeTickRequest) -> None: ...
    wire(LifeTickRequest).fan_out_per(_persona_dicts).to(node(_capture))
    compile_graph()

    with caplog.at_level(logging.WARNING):
        # No exception raised — extractor failure is swallowed + logged.
        await fan_out_life_tick(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert "fan_out_per extractor failed" in caplog.text


@pytest.mark.asyncio
async def test_fan_out_voice_only_at_top_of_hour(reset_runtime, mock_prod, mock_personas):
    from app.nodes.life_dataflow import _persona_dicts, fan_out_voice
    from app.runtime import wire
    from app.runtime.graph import compile_graph
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: VoiceRequest) -> None: seen.append(r)
    wire(VoiceRequest).fan_out_per(_persona_dicts).to(node(_capture))
    compile_graph()

    # 8:30 — wrong minute, no emit
    await fan_out_voice(MinuteTick(ts="2026-04-30T08:30:00+08:00"))
    assert seen == []

    # 8:00 — top of hour in 8..23, emits per persona
    await fan_out_voice(MinuteTick(ts="2026-04-30T08:00:00+08:00"))
    assert {r.persona_id for r in seen} == {"p1", "p2"}

    # 03:00 — top of hour but out of 8..23
    seen.clear()
    await fan_out_voice(MinuteTick(ts="2026-04-30T03:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_life_tick_node_times_out(monkeypatch, caplog):
    import logging

    async def _hang(_persona_id: str):
        await asyncio.sleep(10)

    monkeypatch.setattr("app.life.engine.tick", _hang)
    monkeypatch.setattr("app.nodes.life_dataflow._LIFE_TICK_TIMEOUT_S", 0.01)

    from app.nodes.life_dataflow import life_tick_node

    with caplog.at_level(logging.ERROR):
        await life_tick_node(
            LifeTickRequest(persona_id="p1", ts="2026-04-30T08:00:00+08:00")
        )

    assert "[p1] life_tick timed out" in caplog.text


@pytest.mark.asyncio
async def test_voice_node_times_out(monkeypatch, caplog):
    import logging

    async def _hang(_persona_id: str):
        await asyncio.sleep(10)

    monkeypatch.setattr("app.memory.voice.generate_voice", _hang)
    monkeypatch.setattr("app.nodes.life_dataflow._VOICE_TIMEOUT_S", 0.01)

    from app.nodes.life_dataflow import voice_node

    with caplog.at_level(logging.ERROR):
        await voice_node(
            VoiceRequest(persona_id="p1", ts="2026-04-30T08:00:00+08:00")
        )

    assert "[p1] voice timed out" in caplog.text


@pytest.fixture
def mock_target_groups(monkeypatch):
    monkeypatch.setattr("app.life.glimpse.list_target_groups", lambda: ["chatA", "chatB"])


@pytest.fixture
def mock_random_below_threshold(monkeypatch):
    """random.random() == 0.0 < 0.15 → 命中 15% 抽样."""
    monkeypatch.setattr("app.nodes.life_dataflow.random.random", lambda: 0.0)


@pytest.fixture
def mock_random_above_threshold(monkeypatch):
    monkeypatch.setattr("app.nodes.life_dataflow.random.random", lambda: 0.99)


class _FakeState:
    def __init__(self, activity: str):
        self.activity_type = activity


@pytest.fixture
def mock_life_state(monkeypatch):
    """回拨函数允许测试逐次设置 activity."""
    state_box: dict = {"activity": ""}

    async def _fake_find(_persona_id):
        a = state_box["activity"]
        return _FakeState(a) if a else None
    monkeypatch.setattr("app.data.queries.find_latest_life_state", _fake_find)
    return state_box


@pytest.mark.asyncio
async def test_glimpse_tick_skips_sleeping(reset_runtime, mock_target_groups, mock_life_state):
    from app.nodes.life_dataflow import glimpse_tick_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    mock_life_state["activity"] = "sleeping"
    await glimpse_tick_node(GlimpseTickRequest(persona_id="p1", ts="2026-04-30T10:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_glimpse_tick_browsing_emits_for_each_target(reset_runtime, mock_target_groups, mock_life_state):
    from app.nodes.life_dataflow import glimpse_tick_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list[GlimpseRequest] = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    mock_life_state["activity"] = "browsing"
    await glimpse_tick_node(GlimpseTickRequest(persona_id="p1", ts="2026-04-30T10:00:00+08:00"))
    assert {r.chat_id for r in seen} == {"chatA", "chatB"}
    assert all(r.persona_id == "p1" for r in seen)
    assert all(r.trigger_kind == "tick" for r in seen)


@pytest.mark.asyncio
async def test_glimpse_tick_other_activity_15pct_hit(reset_runtime, mock_target_groups, mock_life_state, mock_random_below_threshold):
    from app.nodes.life_dataflow import glimpse_tick_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    mock_life_state["activity"] = "working"
    await glimpse_tick_node(GlimpseTickRequest(persona_id="p1", ts="2026-04-30T10:00:00+08:00"))
    assert len(seen) == 2  # 两个 chat


@pytest.mark.asyncio
async def test_glimpse_tick_other_activity_15pct_miss(reset_runtime, mock_target_groups, mock_life_state, mock_random_above_threshold):
    from app.nodes.life_dataflow import glimpse_tick_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    mock_life_state["activity"] = "working"
    await glimpse_tick_node(GlimpseTickRequest(persona_id="p1", ts="2026-04-30T10:00:00+08:00"))
    assert seen == []


@pytest.mark.asyncio
async def test_glimpse_event_only_for_browsing(reset_runtime, mock_prod, mock_target_groups):
    from app.nodes.life_dataflow import glimpse_event_node
    from app.runtime import wire
    from app.runtime.node import node

    seen: list = []
    async def _capture(r: GlimpseRequest) -> None: seen.append(r)
    wire(GlimpseRequest).to(node(_capture))

    # 切到 browsing → 触发
    await glimpse_event_node(LifeStateChanged(
        persona_id="p1", activity_type="browsing",
        prev_activity_type="working", ts="2026-04-30T10:00:00+08:00",
    ))
    assert {r.chat_id for r in seen} == {"chatA", "chatB"}
    assert all(r.trigger_kind == "event" for r in seen)

    seen.clear()
    # 切到 working → 不触发
    await glimpse_event_node(LifeStateChanged(
        persona_id="p1", activity_type="working",
        prev_activity_type="browsing", ts="...",
    ))
    assert seen == []

    # 段内 refresh（同 activity）→ 不触发
    await glimpse_event_node(LifeStateChanged(
        persona_id="p1", activity_type="browsing",
        prev_activity_type="browsing", ts="...",
    ))
    assert seen == []


@pytest.mark.asyncio
async def test_run_glimpse_node_does_not_swallow_exception(monkeypatch):
    """durable 节点必须把异常抛出去，让 mq handler nack→DLQ。"""
    from app.nodes.life_dataflow import run_glimpse_node

    async def _boom(_pid, _chat):
        raise RuntimeError("LLM down")
    monkeypatch.setattr("app.life.glimpse.run_glimpse", _boom)

    with pytest.raises(RuntimeError, match="LLM down"):
        await run_glimpse_node(GlimpseRequest(
            request_id="r1", persona_id="p1", chat_id="c1",
            ts="...", trigger_kind="tick",
        ))
