"""world 轮收口的 transcript 沉淀折叠接线（沉淀 Task 2）.

接线合同：``_run_world_round`` 在**成功收口之后**（推进游标 / 标底料 / 排下次醒，
仍在同一 actor 锁串行窗口内）调 ``fold_session(session_id,
build_world_fold_policy(...))``——本轮写回已在 ``Agent.run`` 里落定（两阶段解耦），
折叠是其后的独立步骤；fold_session 整段 fail-open，折叠失败只 log、绝不影响轮。

成本不嵌套污染（spec 决策 5 命门）：折叠调用点在本轮 ``collect_usage`` 作用域之外
——沉淀的 token 落 ``world:sediment`` 独立 actor，绝不算进 world 本体 actor。
"""

from __future__ import annotations

from datetime import datetime

import fakeredis.aioredis
import pytest

import app.agent.sediment as sediment_mod
import app.agent.session_fold as fold_mod
import app.world.engine as engine_mod
from app.agent.neutral import Message, Role
from app.agent.session_fold import (
    FOLD_TRIGGER_MESSAGES,
    FoldPolicy,
    split_fold_message,
)
from app.agent.trace import _accumulate_usage, make_session_id
from app.world.engine import WorldTick, world_tick

_LANE = "coe-t2"


def _today() -> str:
    return datetime.now(engine_mod._CST).strftime("%Y-%m-%d")


def _sid() -> str:
    return make_session_id(_LANE, "world", _today())


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    import app.capabilities.redis as cap_mod
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    monkeypatch.setattr(cap_mod, "_singleton", None)


@pytest.fixture(autouse=True)
def _stub_engine_io(monkeypatch):
    """stub world 节点的 IO（不碰真库），专测折叠接线机制。"""
    state = {
        "close_calls": [],
        "costs": [],   # engine / sediment 两处 record_round_cost 的快照
    }

    async def fake_read_world_state(*, lane):
        from app.world.state import WorldState

        return WorldState(
            lane=lane,
            world_time="2026-06-10T06:30:00+08:00",
            detail="清晨厨房有了动静。",
        )

    async def fake_renotify_unread(*, lane):
        return 0

    async def fake_list_recent_acts(*, lane, cursor_created_at, cursor_act_id, limit):
        return []

    async def fake_record_world_round_close(
        *, lane, advance_cursor_to, materials_ingested_date
    ):
        state["close_calls"].append(
            {"lane": lane, "advance_cursor_to": advance_cursor_to}
        )

    async def fake_find_daily_materials(*, lane, date):
        return None

    async def fake_read_world_arc(*, lane):
        return None

    async def fake_run_arc_reflection(**kwargs):
        pass

    async def fake_load_session(session_id):
        return []

    async def fake_record_round_cost(**kwargs):
        state["costs"].append({**kwargs, "usage": dict(kwargs["usage"])})

    monkeypatch.setattr(engine_mod, "read_world_state", fake_read_world_state)
    monkeypatch.setattr(engine_mod, "renotify_unread", fake_renotify_unread)
    monkeypatch.setattr(engine_mod, "list_recent_acts", fake_list_recent_acts)
    monkeypatch.setattr(
        engine_mod, "record_world_round_close", fake_record_world_round_close
    )
    monkeypatch.setattr(engine_mod, "find_daily_materials", fake_find_daily_materials)
    monkeypatch.setattr(engine_mod, "read_world_arc", fake_read_world_arc)
    monkeypatch.setattr(engine_mod, "run_arc_reflection", fake_run_arc_reflection)
    monkeypatch.setattr(engine_mod, "load_session", fake_load_session)
    monkeypatch.setattr(engine_mod, "record_round_cost", fake_record_round_cost)
    monkeypatch.setattr(sediment_mod, "record_round_cost", fake_record_round_cost)
    return state


def _install_world_agent(monkeypatch, *, usage=None):
    """world 本体 Agent 桩：记录 context（拿本轮 round_id 用）、可注入本轮 usage。"""
    captured: dict = {}

    class _FakeWorldAgent:
        def __init__(self, cfg, *, tools=None, **kwargs):
            pass

        async def run(self, messages, *, prompt_vars=None, context=None,
                      session_id=None, max_retries=2):
            captured["context"] = context
            captured["session_id"] = session_id
            if usage is not None:
                _accumulate_usage(usage)
            return Message(role=Role.ASSISTANT, content="世界往前流了一格")

    monkeypatch.setattr(engine_mod, "Agent", _FakeWorldAgent)
    return captured


def _install_sediment_agent(monkeypatch, *, text="到此刻为止世界这样流过。",
                            usage=None, exc=None):
    captured: dict = {}

    class _FakeSedimentAgent:
        def __init__(self, cfg, *, tools=None, **kwargs):
            captured["cfg"] = cfg

        async def run(self, messages, *, prompt_vars=None, context=None,
                      session_id=None, max_retries=2):
            captured["prompt_vars"] = prompt_vars
            if usage is not None:
                _accumulate_usage(usage)
            if exc is not None:
                raise exc
            return Message(role=Role.ASSISTANT, content=text)

    monkeypatch.setattr(sediment_mod, "Agent", _FakeSedimentAgent)
    return captured


@pytest.fixture
def fold_store(monkeypatch):
    """fold_session 的存取面：默认塞满阈值的历史（旧轮 marker 用与本轮无关的 rid）。"""
    messages: list[Message] = []
    i = 0
    while len(messages) + 2 <= FOLD_TRIGGER_MESSAGES:
        messages.append(
            Message(
                role=Role.USER,
                content=f"[world-round:old-{i:03d}|end:-]\n【现实此刻】12:{i:02d}，看一眼世界。",
            )
        )
        messages.append(Message(role=Role.ASSISTANT, content=f"第 {i} 轮的世界叙述"))
        i += 1
    store = {"messages": messages, "ver": len(messages) // 2, "replaced": None}

    async def fake_load(session_id):
        return list(store["messages"]), store["ver"]

    async def fake_replace(session_id, messages, *, expected_ver=None):
        if expected_ver is not None and expected_ver != store["ver"]:
            return False
        store["replaced"] = list(messages)
        store["ver"] += 1
        return True

    monkeypatch.setattr(fold_mod, "load_session_versioned", fake_load)
    monkeypatch.setattr(fold_mod, "replace_session", fake_replace)
    return store


async def _tick():
    await world_tick(WorldTick(lane=_LANE, reason="heartbeat"))


# ---------------------------------------------------------------------------
# 接线位置：成功收口之后调 fold_session，带正确的 session / 策略参数
# ---------------------------------------------------------------------------


async def test_round_close_folds_with_world_policy(_stub_engine_io, monkeypatch):
    """收口后调 fold_session(world 当日 session, build_world_fold_policy(本轮参数))。"""
    world_captured = _install_world_agent(monkeypatch)

    async def _dummy_writer(prior, rounds):
        return "占位"

    sentinel = FoldPolicy(write_sediment=_dummy_writer)
    policy_calls: list[dict] = []

    def fake_build(**kwargs):
        policy_calls.append(kwargs)
        return sentinel

    fold_calls: list[tuple] = []

    async def fake_fold(session_id, policy):
        fold_calls.append((session_id, policy))
        return False

    monkeypatch.setattr(engine_mod, "build_world_fold_policy", fake_build)
    monkeypatch.setattr(engine_mod, "fold_session", fake_fold)

    await _tick()

    round_id = world_captured["context"].features["world_round_id"]
    assert fold_calls == [(_sid(), sentinel)]
    assert policy_calls == [
        {"lane": _LANE, "session_id": _sid(), "round_id": round_id}
    ]


async def test_fold_runs_after_round_close_before_self_wake(
    _stub_engine_io, monkeypatch
):
    """折叠在成本入账 + 收口（推进游标）之后、**排下次醒之前**。

    fold 必须先于 fire_self_wake（codex T3 必改 1）：沉淀 LLM 最长 120s 仍占着
    actor 单飞锁，若先排 self-wake，短延迟（最短 60s）的自排会在折叠期间到达
    撞锁被吞。fold 完成后才开始给下一轮自排计时，窗口消失。
    """
    order: list[str] = []

    async def fake_cost(**kwargs):
        order.append("round_cost")

    async def fake_close(*, lane, advance_cursor_to, materials_ingested_date):
        order.append("round_close")

    async def fake_fold(session_id, policy):
        order.append("fold")
        return False

    async def fake_self_wake(*, lane, self_wake):
        order.append("self_wake")
        return False

    monkeypatch.setattr(engine_mod, "record_round_cost", fake_cost)
    monkeypatch.setattr(engine_mod, "record_world_round_close", fake_close)
    monkeypatch.setattr(engine_mod, "fold_session", fake_fold)
    monkeypatch.setattr(engine_mod, "fire_self_wake", fake_self_wake)
    _install_world_agent(monkeypatch)

    await _tick()

    assert order == ["round_cost", "round_close", "fold", "self_wake"]


# ---------------------------------------------------------------------------
# 成本不嵌套污染：沉淀 usage 绝不算进 world 本体 actor
# ---------------------------------------------------------------------------


async def test_sediment_usage_never_pollutes_world_actor(
    _stub_engine_io, monkeypatch, fold_store
):
    """端到端（真 fold_session + 真沉淀回调）：两份成本各归各的 actor。"""
    world_captured = _install_world_agent(
        monkeypatch, usage={"input": 2000, "output": 80, "total": 2080}
    )
    sed_captured = _install_sediment_agent(
        monkeypatch,
        text="到此刻为止世界这样流过。",
        usage={"input": 70, "output": 7, "total": 77},
    )

    await _tick()

    costs = _stub_engine_io["costs"]
    by_actor = {c["actor"]: c for c in costs}
    assert by_actor["world"]["usage"]["input"] == 2000, (
        "world 本体 actor 的 usage 不得混入沉淀的 70"
    )
    assert by_actor["world"]["usage"]["calls"] == 1
    sed = by_actor["world:sediment"]
    assert sed["usage"]["input"] == 70
    assert sed["lane"] == _LANE
    round_id = world_captured["context"].features["world_round_id"]
    assert sed["round_id"] == sediment_mod._sediment_round_id(_sid(), round_id)

    # world 口吻：零 prompt_vars + world_sediment 配置
    assert sed_captured["prompt_vars"] == {}
    assert sed_captured["cfg"] is sediment_mod._WORLD_SEDIMENT_CFG

    # 折叠真落库：单条折叠消息 = 沉淀正文 + 旧轮 marker 逐行保全
    assert fold_store["replaced"] is not None and len(fold_store["replaced"]) == 1
    sediment, markers = split_fold_message(fold_store["replaced"][0])
    assert sediment == "到此刻为止世界这样流过。"
    assert len(markers) == FOLD_TRIGGER_MESSAGES // 2


async def test_sediment_failure_never_kills_world_round(
    _stub_engine_io, monkeypatch, fold_store, caplog
):
    """沉淀失败 → 本轮收口照常（已推进收口）、transcript 原样不动、只 warning 不抛。"""
    _install_world_agent(monkeypatch)
    _install_sediment_agent(monkeypatch, exc=RuntimeError("llm down"))

    with caplog.at_level("WARNING"):
        await _tick()  # 不抛

    assert len(_stub_engine_io["close_calls"]) == 1, "收口在折叠之前已经完成"
    assert fold_store["replaced"] is None, "折叠失败本版不折、原样不动"
    assert any("fold" in r.message.lower() for r in caplog.records)
