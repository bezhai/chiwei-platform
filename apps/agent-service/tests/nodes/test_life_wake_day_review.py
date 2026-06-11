"""life 轮收口的睡前回顾快班触发 + 昨天页 life 侧注入（睡前回顾 Task 2）.

快班合同：life 轮收口处——**本轮最新** LifeState.activity_type == "sleep" 且
``day_reviewed_date != living_day(now)`` → 就地跑一次睡前回顾（``run_day_review``
自身 fail-open + single_flight，绝不杀 life 轮；主保证仍是凌晨对账 cron）。

life 侧注入合同：每轮 stimulus 注入「她最近一页昨天」——marker
（``day_reviewed_date``）记着最近回顾过的生活日，按它取那天的昨天页；没回顾过
（marker None）/ 页缺失 → 整段缺席不补占位。位置在稳定前缀区（时刻行之前）。

这些是节点编排测试：Agent.run / run_day_review 都打桩，验证触发条件与注入
形态，不验证 LLM 想得对。
"""

from __future__ import annotations

import datetime as _dt

import fakeredis.aioredis
import pytest

import app.nodes.life_wake as lw
from app.agent.neutral import Message, Role
from app.domain.life_state import LifeState
from app.domain.world_events import EventArrived, EventEnvelope
from app.life.pages import DayPage

_LANE = "coe-t3"
_PERSONA = "akao"


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    return fake


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    """钉死 now = 2026-06-10 23:30 CST（快班典型场景：当晚入睡）。"""

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 10, 23, 30, tzinfo=tz)

    monkeypatch.setattr(lw.cst_time, "datetime", _FixedDateTime)


def _envelope(event_id="e1", summary="水壶在响"):
    return EventEnvelope(
        lane=_LANE,
        persona_id=_PERSONA,
        event_id=event_id,
        kind="ambient",
        source="world",
        summary=summary,
        occurred_at="2026-06-10T23:00:00+08:00",
    )


def _snapshot(**kwargs) -> LifeState:
    base = {
        "lane": _LANE,
        "persona_id": _PERSONA,
        "current_state": "躺下睡了",
        "response_mood": "平静",
        "activity_type": "sleep",
        "observed_at": "2026-06-10T23:30:00+08:00",
    }
    base.update(kwargs)
    return LifeState(**base)


class _FakeAgent:
    instances: list = []

    def __init__(self, cfg, *, tools=None, **kwargs):
        self.cfg = cfg
        self.tools = tools or []
        self.run_calls: list[dict] = []
        _FakeAgent.instances.append(self)

    @classmethod
    def install(cls, monkeypatch):
        cls.instances = []
        monkeypatch.setattr(lw, "Agent", cls)
        return cls

    async def run(self, messages, *, prompt_vars=None, context=None,
                  session_id=None, max_retries=None):
        self.run_calls.append({"messages": messages})
        return Message(role=Role.ASSISTANT, content="ok")

    @classmethod
    def last_stimulus(cls) -> str:
        assert cls.instances and cls.instances[-1].run_calls
        return cls.instances[-1].run_calls[-1]["messages"][0].text()


@pytest.fixture
def patched(monkeypatch):
    """stub 节点 IO；run_day_review / read_day_page 打桩记录调用。"""
    state = {
        "snapshot": _snapshot(),
        "unread": [_envelope()],
        "transcript": [],
        "marked": [],
        "reviews": [],          # run_day_review 收到的调用
        "day_page": None,       # read_day_page 返回
        "page_reads": [],       # read_day_page 收到的 (lane, persona, date)
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
        from app.memory._persona import PersonaContext

        return PersonaContext(
            persona_id=persona_id, display_name=persona_id, persona_lite="人设"
        )

    async def fake_review(**kwargs):
        state["reviews"].append(kwargs)

    async def fake_read_day_page(*, lane, persona_id, date):
        state["page_reads"].append({"lane": lane, "persona_id": persona_id, "date": date})
        return state["day_page"]

    async def fake_arc(*, lane):
        return state["arc"]

    import app.domain.arc_awareness as arc_mod

    monkeypatch.setattr(arc_mod, "read_world_arc", fake_arc)
    monkeypatch.setattr(lw, "find_life_state", fake_find)
    monkeypatch.setattr(lw, "list_unread_events", fake_unread)
    monkeypatch.setattr(lw, "mark_events_read", fake_mark)
    monkeypatch.setattr(lw, "load_persona", fake_load_persona)
    monkeypatch.setattr(lw, "load_session", fake_load_session)
    monkeypatch.setattr(lw, "run_day_review", fake_review)
    monkeypatch.setattr(lw, "read_day_page", fake_read_day_page)
    return state


async def _wake():
    await lw.life_wake_node(EventArrived(lane=_LANE, persona_id=_PERSONA))


# ---------------------------------------------------------------------------
# 快班触发：sleep 且生活日未回顾才跑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_close_triggers_day_review(patched, monkeypatch):
    """收口最新快照是 sleep、生活日未回顾 → 触发回顾（target=living_day(now)）。"""
    patched["snapshot"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert len(patched["reviews"]) == 1
    call = patched["reviews"][0]
    assert call["lane"] == _LANE
    assert call["persona_id"] == _PERSONA
    assert call["target_date"] == "2026-06-10", "23:30 入睡回看的是当日生活日"
    # trace 归组用她当天的意识流 session id（只做标签，不续接——回顾自己无会话）
    from app.agent.trace import make_session_id

    assert call["trace_session_id"] == make_session_id(_LANE, _PERSONA, "2026-06-10")


@pytest.mark.asyncio
async def test_non_sleep_close_does_not_trigger(patched, monkeypatch):
    """收口快照不是 sleep（她还醒着）→ 不触发（睡前回顾等她真去睡）。"""
    patched["snapshot"] = _snapshot(activity_type="study", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert patched["reviews"] == []


@pytest.mark.asyncio
async def test_already_reviewed_living_day_does_not_trigger(patched, monkeypatch):
    """同生活日已回顾（起夜 03:50 再睡）→ marker 挡住，不重复触发。"""
    patched["snapshot"] = _snapshot(activity_type="sleep", day_reviewed_date="2026-06-10")
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert patched["reviews"] == []


@pytest.mark.asyncio
async def test_no_snapshot_does_not_trigger(patched, monkeypatch):
    """从没写过快照（她一轮都没 update 过）→ 没有 sleep 声明，不触发。"""
    patched["snapshot"] = None
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert patched["reviews"] == []


@pytest.mark.asyncio
async def test_late_night_sleep_targets_previous_living_day(patched, monkeypatch):
    """熬夜 01:30 入睡：living_day 归前一日 → 回看的是前一日的生活日。"""

    class _LateNight(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 11, 1, 30, tzinfo=tz)

    monkeypatch.setattr(lw.cst_time, "datetime", _LateNight)
    patched["snapshot"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert len(patched["reviews"]) == 1
    assert patched["reviews"][0]["target_date"] == "2026-06-10", (
        "熬夜 01:30 入睡回看的是前一日生活日（钟的约定）"
    )


@pytest.mark.asyncio
async def test_review_runs_after_round_closure(patched, monkeypatch):
    """回顾在收口（标已读）之后才跑——它失败 / 慢都不影响这一轮的 durable 收口。"""
    order: list[str] = []

    async def fake_mark(*, lane, persona_id, event_ids):
        order.append("mark_read")

    async def fake_review(**kwargs):
        order.append("review")

    monkeypatch.setattr(lw, "mark_events_read", fake_mark)
    monkeypatch.setattr(lw, "run_day_review", fake_review)
    patched["snapshot"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert order == ["mark_read", "review"]


# ---------------------------------------------------------------------------
# life 侧注入：她最近一页昨天进 stimulus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stimulus_injects_latest_day_page(patched, monkeypatch):
    """marker 记着最近回顾过的生活日 → 那天的昨天页进 stimulus（带日期、在时刻行之前）。"""
    patched["snapshot"] = _snapshot(
        activity_type="study", day_reviewed_date="2026-06-09"
    )
    patched["day_page"] = DayPage(
        lane=_LANE,
        persona_id=_PERSONA,
        date="2026-06-09",
        narrative="昨天最挂心的是那道没解出来的题。",
        written_at="2026-06-09T23:40:00+08:00",
    )
    _FakeAgent.install(monkeypatch)

    await _wake()

    stimulus = _FakeAgent.last_stimulus()
    assert "昨天最挂心的是那道没解出来的题。" in stimulus
    assert "2026-06-09" in stimulus, "注入段要标明这页写的是哪一天"
    # 位置：稳定前缀区（每轮都变的时刻行之前）
    assert stimulus.index("昨天最挂心") < stimulus.index("现在是"), (
        "昨天页该在时刻行之前（稳定前缀区，页天级才变）"
    )
    # 取的就是 marker 指着的那天
    assert patched["page_reads"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "date": "2026-06-09"}
    ]


@pytest.mark.asyncio
async def test_stimulus_no_day_page_when_never_reviewed(patched, monkeypatch):
    """没回顾过（marker None）→ 整段缺席、不读页、不补占位（诚实的真空）。"""
    patched["snapshot"] = _snapshot(activity_type="study", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert patched["page_reads"] == [], "marker None 不该去读页"
    assert "上一页" not in _FakeAgent.last_stimulus()


@pytest.mark.asyncio
async def test_stimulus_no_day_page_when_page_missing(patched, monkeypatch):
    """marker 在但页读不到（异常情形）→ 整段缺席，不渲染空页框架。"""
    patched["snapshot"] = _snapshot(
        activity_type="study", day_reviewed_date="2026-06-09"
    )
    patched["day_page"] = None
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert "上一页" not in _FakeAgent.last_stimulus()


@pytest.mark.asyncio
async def test_stimulus_no_day_page_when_no_snapshot(patched, monkeypatch):
    """没有 LifeState（首轮）→ 注入整段缺席（不读页）。"""
    patched["snapshot"] = None
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert patched["page_reads"] == []


@pytest.mark.asyncio
async def test_stimulus_day_page_read_failure_does_not_kill_round(
    patched, monkeypatch, caplog
):
    """读页抛错 → 绝不杀 life 轮：本轮照常 run + 收口，注入段缺席、warning 留痕
    （照 render_arc_awareness 的姿势：注入是上下文增强，失败只 log）。"""
    patched["snapshot"] = _snapshot(
        activity_type="study", day_reviewed_date="2026-06-09"
    )

    async def boom_read_day_page(*, lane, persona_id, date):
        raise RuntimeError("pg down while reading day page")

    monkeypatch.setattr(lw, "read_day_page", boom_read_day_page)
    _FakeAgent.install(monkeypatch)

    with caplog.at_level("WARNING"):
        await _wake()  # 不抛

    assert _FakeAgent.instances and _FakeAgent.instances[-1].run_calls, (
        "读页失败本轮必须照常跑"
    )
    assert "上一页" not in _FakeAgent.last_stimulus(), "失败时注入段整段缺席"
    assert patched["marked"] == [["e1"]], "本轮照常收口标已读"
    assert any(r.levelname == "WARNING" for r in caplog.records)


# ---------------------------------------------------------------------------
# sleep 引导：update_life_state 的工具说明轻补一句（prompt 姿态，不立规则）
# ---------------------------------------------------------------------------


def test_update_life_state_doc_guides_sleep_marking():
    """update_life_state 工具说明引导"去睡时把 activity_type 标成 sleep"。"""
    from app.nodes.life_tools import build_life_tools

    tools = build_life_tools(
        lane=_LANE, persona_id=_PERSONA, act_id="a", observed_at="t"
    )
    update = next(t for t in tools if t.name == "update_life_state")
    desc = update.definition.description
    assert "sleep" in desc
    assert "去睡" in desc, "工具说明要轻补一句去睡标 sleep 的引导（prompt 姿态）"
