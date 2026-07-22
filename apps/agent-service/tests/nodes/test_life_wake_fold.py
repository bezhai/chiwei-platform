"""life 轮收口的 transcript 沉淀折叠接线（沉淀 Task 2）.

接线合同：``_run_life_round`` 在**全部 durable 收口之后**（标已读 / 排下次醒 / cd，
仍在同一单飞锁串行窗口内）、睡前回顾之前，调 ``fold_session(session_id,
build_life_fold_policy(...))``——本轮写回已在 ``Agent.run`` 里落定（两阶段解耦），
折叠是其后的独立步骤；fold_session 整段 fail-open，折叠失败只 log、绝不影响轮。

成本不嵌套污染（spec 决策 5 命门）：折叠调用点在本轮 ``collect_usage`` 作用域之外
——沉淀的 token 落 ``{persona}:sediment`` 独立 actor，绝不算进 life 本体 actor。

这些是节点编排测试：life / 沉淀两个 Agent.run 都打桩，验证接线位置与成本隔离，
不验证 LLM 想得对。
"""

from __future__ import annotations

import datetime as _dt

import fakeredis.aioredis
import pytest

import app.agent.sediment as sediment_mod
import app.agent.session_fold as fold_mod
import app.capabilities.redis as redis_capability
import app.nodes.life_wake as lw
from app.agent.neutral import Message, Role
from app.agent.session_fold import (
    FOLD_TRIGGER_MESSAGES,
    FoldPolicy,
    split_fold_message,
)
from app.agent.trace import _accumulate_usage, make_session_id
from app.domain.life_state import LifeState
from app.domain.world_events import EventArrived, EventEnvelope
from app.memory._persona import PersonaContext

_LANE = "coe-t2"
_PERSONA = "akao"
_TODAY = "2026-06-10"
_SID = make_session_id(_LANE, _PERSONA, _TODAY)


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    monkeypatch.setattr(redis_capability, "_singleton", None)
    return fake


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """钉死 now = 2026-06-10 14:30 CST（午后普通一轮，不触发睡前回顾）。"""

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 10, 14, 30, tzinfo=tz)

    monkeypatch.setattr(lw.cst_time, "datetime", _FixedDateTime)


def _envelope(event_id="e1", summary="水壶在响"):
    return EventEnvelope(
        lane=_LANE,
        persona_id=_PERSONA,
        event_id=event_id,
        kind="ambient",
        source="world",
        summary=summary,
        occurred_at="2026-06-10T14:00:00+08:00",
    )


def _snapshot(**kwargs) -> LifeState:
    base = {
        "lane": _LANE,
        "persona_id": _PERSONA,
        "current_state": "在写作业",
        "response_mood": "平静",
        "activity_type": "study",
        "observed_at": "2026-06-10T14:00:00+08:00",
    }
    base.update(kwargs)
    return LifeState(**base)


def _life_rounds(n_messages: int) -> list[Message]:
    """凑 n 条折叠存储里的历史消息（带 life round marker，2 条/轮）。"""
    out: list[Message] = []
    i = 0
    while len(out) + 2 <= n_messages:
        out.append(
            Message(
                role=Role.USER,
                content=f"[life-round:old-{i:03d}]\n现在是 12:{i:02d}。第 {i} 件动静。",
            )
        )
        out.append(Message(role=Role.ASSISTANT, content=f"我想了想第 {i} 件事"))
        i += 1
    return out


@pytest.fixture
def patched(monkeypatch):
    """stub 节点 IO（不碰真库），专测折叠接线机制。"""
    state = {
        "snapshot": _snapshot(),
        "unread": [_envelope()],
        "transcript": [],   # lw.load_session（turn 幂等查重读的那份）
        "marked": [],
        "costs": [],        # lw / sediment 两处 record_round_cost 的快照
        "arc": None,
    }

    async def fake_find(*, lane, persona_id):
        return state["snapshot"]

    async def fake_unread(*, lane, persona_id):
        return list(state["unread"])

    async def fake_load_session(session_id, **kwargs):
        return list(state["transcript"])

    async def fake_mark(*, lane, persona_id, event_ids):
        state["marked"].append(event_ids)

    async def fake_load_persona(persona_id):
        return PersonaContext(
            persona_id=persona_id, display_name=persona_id, persona_lite="人设"
        )

    async def fake_review(**kwargs):
        pass

    async def fake_read_day_page_before(*, lane, persona_id, before_date):
        return None

    async def fake_arc(*, lane):
        return state["arc"]

    async def fake_record_round_cost(**kwargs):
        state["costs"].append({**kwargs, "usage": dict(kwargs["usage"])})

    import app.domain.arc_awareness as arc_mod

    monkeypatch.setattr(arc_mod, "read_world_arc", fake_arc)
    monkeypatch.setattr(lw, "find_life_state", fake_find)
    monkeypatch.setattr(lw, "list_unread_events", fake_unread)
    monkeypatch.setattr(lw, "mark_events_read", fake_mark)
    monkeypatch.setattr(lw, "load_persona", fake_load_persona)
    monkeypatch.setattr(lw, "load_session", fake_load_session)
    monkeypatch.setattr(lw, "run_day_review", fake_review)
    monkeypatch.setattr(lw, "read_day_page_before", fake_read_day_page_before)
    monkeypatch.setattr(lw, "record_round_cost", fake_record_round_cost)
    monkeypatch.setattr(sediment_mod, "record_round_cost", fake_record_round_cost)
    monkeypatch.setattr(sediment_mod, "load_persona", fake_load_persona)
    return state


def _install_life_agent(monkeypatch, *, usage=None):
    """life 本体 Agent 桩：可注入本轮 usage（进当前 collect_usage 作用域）。"""

    class _FakeLifeAgent:
        def __init__(self, cfg, *, tools=None, **kwargs):
            pass

        async def run(self, messages, *, prompt_vars=None, context=None,
                      session_id=None, max_retries=2):
            if usage is not None:
                _accumulate_usage(usage)
            return Message(role=Role.ASSISTANT, content="过我自己的一刻")

    monkeypatch.setattr(lw, "Agent", _FakeLifeAgent)


def _install_sediment_agent(monkeypatch, *, text="到此刻为止我记得这些。", usage=None,
                            exc=None):
    class _FakeSedimentAgent:
        def __init__(self, cfg, *, tools=None, **kwargs):
            pass

        async def run(self, messages, *, prompt_vars=None, context=None,
                      session_id=None, max_retries=2):
            if usage is not None:
                _accumulate_usage(usage)
            if exc is not None:
                raise exc
            return Message(role=Role.ASSISTANT, content=text)

    monkeypatch.setattr(sediment_mod, "Agent", _FakeSedimentAgent)


@pytest.fixture
def fold_store(monkeypatch):
    """fold_session 的存取面（与 lw.load_session 的 turn 幂等查重读取面是两回事）。"""
    store = {
        "messages": _life_rounds(FOLD_TRIGGER_MESSAGES),
        "ver": FOLD_TRIGGER_MESSAGES // 2,
        "replaced": None,
    }

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


async def _wake():
    await lw.life_wake_node(EventArrived(lane=_LANE, persona_id=_PERSONA))


def _expected_round_id() -> str:
    return lw._derive_life_round_id(
        lane=_LANE,
        persona_id=_PERSONA,
        read_ids=["e1"],
    )


# ---------------------------------------------------------------------------
# 接线位置：收口后调 fold_session，带正确的 session / 策略参数
# ---------------------------------------------------------------------------


async def test_round_close_folds_with_life_policy(patched, monkeypatch):
    """收口后调 fold_session(本日 session, build_life_fold_policy(本轮参数))。"""
    _install_life_agent(monkeypatch)

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

    monkeypatch.setattr(lw, "build_life_fold_policy", fake_build)
    monkeypatch.setattr(lw, "fold_session", fake_fold)

    await _wake()

    assert fold_calls == [(_SID, sentinel)]
    assert policy_calls == [
        {
            "lane": _LANE,
            "persona_id": _PERSONA,
            "session_id": _SID,
            "round_id": _expected_round_id(),
        }
    ]


async def test_fold_runs_after_mark_read_before_reminders_and_review(
    patched, monkeypatch
):
    """折叠在成本入账 + 标已读之后、挂日程到点提醒之前、睡前回顾之前。

    收口顺序（Task 2 删自设闹钟后）：round_cost → mark_read → fold →
    fire_schedule_reminders → review。沉淀 LLM 最长 120s 仍占着单飞锁，先把这一步做完
    再挂日程提醒，收口顺序稳定。
    """
    order: list[str] = []

    async def fake_cost(**kwargs):
        order.append("round_cost")

    async def fake_mark(*, lane, persona_id, event_ids):
        order.append("mark_read")

    async def fake_fold(session_id, policy):
        order.append("fold")
        return False

    async def fake_fire_reminders(*, lane, persona_id, schedule_reminders):
        order.append("reminders")
        return 0

    async def fake_review(**kwargs):
        order.append("review")

    monkeypatch.setattr(lw, "record_round_cost", fake_cost)
    monkeypatch.setattr(lw, "mark_events_read", fake_mark)
    monkeypatch.setattr(lw, "fold_session", fake_fold)
    monkeypatch.setattr(lw, "fire_schedule_reminders", fake_fire_reminders)
    monkeypatch.setattr(lw, "run_day_review", fake_review)
    # 边沿触发铺设：轮始醒着（第一次读）、收口最新快照是 sleep（第二次读）——
    # 本轮发生「进入睡眠」的转变，review 才会跑（快班是边沿触发不是电平）。
    find_calls = {"n": 0}

    async def fake_find(*, lane, persona_id):
        find_calls["n"] += 1
        if find_calls["n"] == 1:
            return _snapshot(activity_type="study", day_reviewed_date=None)
        return _snapshot(activity_type="sleep", day_reviewed_date=None)

    monkeypatch.setattr(lw, "find_life_state", fake_find)
    _install_life_agent(monkeypatch)

    await _wake()

    assert order == ["round_cost", "mark_read", "fold", "reminders", "review"]


async def test_turn_idempotent_skip_does_not_fold(patched, monkeypatch):
    """同轮重投命中 turn 幂等 → 整段早返，不折叠（折叠等真跑过的轮收口再做）。"""
    marker = lw._round_marker(_expected_round_id())
    patched["transcript"] = [Message(role=Role.USER, content=f"{marker}\n上一次的本轮")]
    fold_calls: list = []

    async def fake_fold(session_id, policy):
        fold_calls.append(session_id)
        return False

    monkeypatch.setattr(lw, "fold_session", fake_fold)
    _install_life_agent(monkeypatch)

    await _wake()

    assert fold_calls == []
    assert patched["marked"] == [], "幂等跳过的轮不收口（现状语义）"


# ---------------------------------------------------------------------------
# 成本不嵌套污染：沉淀 usage 绝不算进 life 本体 actor
# ---------------------------------------------------------------------------


async def test_sediment_usage_never_pollutes_life_actor(
    patched, monkeypatch, fold_store
):
    """端到端（真 fold_session + 真沉淀回调）：两份成本各归各的 actor。"""
    _install_life_agent(
        monkeypatch, usage={"input": 1000, "output": 50, "total": 1050}
    )
    _install_sediment_agent(
        monkeypatch,
        text="到此刻为止我记得这些。",
        usage={"input": 70, "output": 7, "total": 77},
    )

    await _wake()

    by_actor = {c["actor"]: c for c in patched["costs"]}
    assert by_actor[_PERSONA]["usage"]["input"] == 1000, (
        "life 本体 actor 的 usage 不得混入沉淀的 70"
    )
    assert by_actor[_PERSONA]["usage"]["calls"] == 1
    sed = by_actor[f"{_PERSONA}:sediment"]
    assert sed["usage"]["input"] == 70
    assert sed["lane"] == _LANE
    assert sed["round_id"] == sediment_mod._sediment_round_id(
        _SID, _expected_round_id()
    )

    # 折叠真落库：单条折叠消息 = 沉淀正文 + marker 逐行保全
    assert fold_store["replaced"] is not None and len(fold_store["replaced"]) == 1
    sediment, markers = split_fold_message(fold_store["replaced"][0])
    assert sediment == "到此刻为止我记得这些。"
    assert len(markers) == FOLD_TRIGGER_MESSAGES // 2


async def test_sediment_failure_never_kills_the_round(
    patched, monkeypatch, fold_store, caplog
):
    """沉淀失败 → 本轮收口照常（已标已读）、transcript 原样不动、只 warning 不抛。"""
    _install_life_agent(monkeypatch)
    _install_sediment_agent(monkeypatch, exc=RuntimeError("llm down"))

    with caplog.at_level("WARNING"):
        await _wake()  # 不抛

    assert patched["marked"] == [["e1"]], "本轮 durable 收口在折叠之前已经完成"
    assert fold_store["replaced"] is None, "折叠失败本版不折、原样不动"
    assert any("fold" in r.message.lower() for r in caplog.records)
