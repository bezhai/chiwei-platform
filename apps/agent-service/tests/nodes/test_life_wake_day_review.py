"""life 轮收口的睡前回顾快班触发 + 昨天页 life 侧注入（睡前回顾 Task 2）.

快班合同（事故修复后语义）：life 轮收口处——本轮发生了「进入睡眠」的**转变**
（轮开始的快照不是 sleep、收口最新快照是 sleep）→ 就地跑一次睡前回顾
（trigger="sleep"，**无 marker 预检查**：每次入睡都回顾当前生活日，午睡 /
回笼觉产生中间版、后一次整篇盖前一次是设计行为）。触发条件是边沿不是电平：
她睡着时夜里被群消息 / self-wake 吵醒跑的轮（轮始轮末都是 sleep）不再各跑一次
回顾——旧电平触发会让成本随夜间打扰线性放大。``run_day_review`` 自身 fail-open
+ single_flight 防并发撞车，绝不杀 life 轮；主保证仍是凌晨对账 cron。

life 侧注入合同：每轮 stimulus 注入「她最近一页昨天」——取日期**严格早于**
当前生活日的最新一版日页（不读 marker：清晨回笼觉后 marker 会被快班推前到
「今天」、对账班补旧日还会回拨）；没有更早的页 → 整段缺席不补占位。位置在
稳定前缀区（时刻行之前）。

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
        self.run_calls.append({"messages": messages, "prompt_vars": prompt_vars})
        return Message(role=Role.ASSISTANT, content="ok")

    @classmethod
    def last_stimulus(cls) -> str:
        assert cls.instances and cls.instances[-1].run_calls
        return cls.instances[-1].run_calls[-1]["messages"][0].text()

    @classmethod
    def last_prompt_vars(cls) -> dict:
        assert cls.instances and cls.instances[-1].run_calls
        return cls.instances[-1].run_calls[-1]["prompt_vars"]


# latest 未单独铺设时的哨兵：收口现读与轮始快照相同（本轮没 update 过状态）。
_MIRROR = object()


@pytest.fixture
def patched(monkeypatch):
    """stub 节点 IO；run_day_review / read_day_page_before 打桩记录调用。

    ``find_life_state`` 在一轮里被读两次：轮开始（gate 段）读到 ``snapshot``、
    收口现读读到 ``latest``（本轮 update 过之后的最新快照）。``latest`` 不铺设
    （_MIRROR）时与 ``snapshot`` 相同——本轮没改过状态。
    """
    state = {
        "snapshot": _snapshot(),
        "latest": _MIRROR,      # 收口现读的最新快照（铺设转变场景用）
        "find_calls": 0,
        "unread": [_envelope()],
        "transcript": [],
        "marked": [],
        "reviews": [],          # run_day_review 收到的调用
        "day_page": None,       # read_day_page_before 返回
        "page_reads": [],       # read_day_page_before 收到的 (lane, persona, before_date)
        "arc": None,
    }

    async def fake_find(*, lane, persona_id):
        state["find_calls"] += 1
        if state["find_calls"] == 1:
            return state["snapshot"]
        return state["snapshot"] if state["latest"] is _MIRROR else state["latest"]

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

    async def fake_read_day_page_before(*, lane, persona_id, before_date):
        state["page_reads"].append(
            {"lane": lane, "persona_id": persona_id, "before_date": before_date}
        )
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
    monkeypatch.setattr(lw, "read_day_page_before", fake_read_day_page_before)
    return state


async def _wake():
    await lw.life_wake_node(EventArrived(lane=_LANE, persona_id=_PERSONA))


# ---------------------------------------------------------------------------
# 快班触发：「进入睡眠」的转变才跑（边沿，不是电平；无 marker 闸）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entering_sleep_triggers_day_review(patched, monkeypatch):
    """轮始醒着、收口最新快照是 sleep（本轮发生「进入睡眠」转变）→ 触发回顾
    （target=living_day(now)、trigger="sleep"）。"""
    patched["snapshot"] = _snapshot(
        activity_type="study", current_state="在写作业", day_reviewed_date=None
    )
    patched["latest"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert len(patched["reviews"]) == 1
    call = patched["reviews"][0]
    assert call["lane"] == _LANE
    assert call["persona_id"] == _PERSONA
    assert call["target_date"] == "2026-06-10", "23:30 入睡回看的是当日生活日"
    assert call["trigger"] == "sleep", "快班必须亮明触发源（无 marker 闸）"
    # trace 归组用她当天的意识流 session id（只做标签，不续接——回顾自己无会话）
    from app.agent.trace import make_session_id

    assert call["trace_session_id"] == make_session_id(_LANE, _PERSONA, "2026-06-10")


@pytest.mark.asyncio
async def test_asleep_through_round_does_not_retrigger(patched, monkeypatch):
    """轮始已是 sleep、轮末仍是 sleep（睡梦中被群消息 / self-wake 吵了一轮但没
    醒透）→ **不**触发：触发条件是「进入睡眠」的转变（边沿），不是「正在睡」
    的状态（电平）。电平触发会让每个夜间被吵的轮都再跑一次回顾、成本随打扰
    线性放大（旧 marker 闸拆掉后暴露的洞）。"""
    patched["snapshot"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
    patched["latest"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert patched["reviews"] == []


@pytest.mark.asyncio
async def test_non_sleep_close_does_not_trigger(patched, monkeypatch):
    """收口快照不是 sleep（她还醒着）→ 不触发（睡前回顾等她真去睡）。"""
    patched["snapshot"] = _snapshot(activity_type="study", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert patched["reviews"] == []


@pytest.mark.asyncio
async def test_resleep_same_living_day_triggers_again(patched, monkeypatch):
    """同生活日先睡→醒→再睡（起夜 / 回笼觉）：这一轮她从醒着到再入睡，是合法
    的「进入睡眠」转变 → **仍然触发**（哪怕 marker == 当日也不挡：每次入睡都
    回顾当前生活日，后一次整篇盖前一次是设计行为，事故修复——旧 marker 闸在
    这里会把回笼觉挡掉、却在清晨把 marker 推前坑了对账班）。"""
    patched["snapshot"] = _snapshot(
        activity_type="move",
        current_state="起夜回来准备再躺下",
        day_reviewed_date="2026-06-10",
    )
    patched["latest"] = _snapshot(
        activity_type="sleep", day_reviewed_date="2026-06-10"
    )
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert len(patched["reviews"]) == 1
    assert patched["reviews"][0]["trigger"] == "sleep"


@pytest.mark.asyncio
async def test_first_round_entering_sleep_triggers(patched, monkeypatch):
    """轮始还没有快照（她第一轮活）、本轮 update 成 sleep → 也是「进入睡眠」
    的转变，照常触发。"""
    patched["snapshot"] = None
    patched["latest"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert len(patched["reviews"]) == 1
    assert patched["reviews"][0]["trigger"] == "sleep"


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
    patched["snapshot"] = _snapshot(activity_type="study", day_reviewed_date=None)
    patched["latest"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
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
    patched["snapshot"] = _snapshot(activity_type="study", day_reviewed_date=None)
    patched["latest"] = _snapshot(activity_type="sleep", day_reviewed_date=None)
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert order == ["mark_read", "review"]


# ---------------------------------------------------------------------------
# life 侧注入：她最近一页昨天进 **prompt_vars[day_page]**（system prompt，每轮注入），
# 不进 USER stimulus。取严格早于当前生活日的页，不读 marker。改放 system 而非 USER 的
# 根因：当天 transcript 会被 fold 压掉这段（昨天页不是「今天经历」、fold 会概括掉它）、
# 不能靠继承——所以每轮注入到 prompt_vars，不怕 fold、确定可见、稳定走 prompt cache。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_day_page_strictly_before_current_living_day_to_prompt_vars(
    patched, monkeypatch
):
    """注入读口按「日期严格早于当前生活日」取页、完全不看 marker，结果进 prompt_vars[day_page]、
    不进 USER stimulus。铺设清晨回笼觉后的坑场景：marker 已被快班推前到「今天」——旧读法
    会把当天凌晨刚写的短页错当「上一页日子」；新读法按当前生活日做上界，拿到的是昨天那页。"""
    patched["snapshot"] = _snapshot(
        activity_type="study", day_reviewed_date="2026-06-10"  # marker 指着今天
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

    day_page = _FakeAgent.last_prompt_vars()["day_page"]
    assert "昨天最挂心的是那道没解出来的题。" in day_page
    assert "2026-06-09" in day_page, "注入段要标明这页写的是哪一天"
    # 不进 USER stimulus
    assert "昨天最挂心" not in _FakeAgent.last_stimulus(), "昨天页不再进 USER stimulus"
    # 读口收到的是「当前生活日」上界（now=2026-06-10 23:30 → 生活日 2026-06-10），
    # 而不是 marker 指着的日期。
    assert patched["page_reads"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "before_date": "2026-06-10"}
    ]


@pytest.mark.asyncio
async def test_day_page_to_prompt_vars_even_when_marker_absent(patched, monkeypatch):
    """marker None（她自己没回顾过）但库里有更早的页（对账班补的）→ 照常注入 prompt_vars：
    读口只认页的存在，marker 已降级为观测留痕、不再是指针。"""
    patched["snapshot"] = _snapshot(activity_type="study", day_reviewed_date=None)
    patched["day_page"] = DayPage(
        lane=_LANE,
        persona_id=_PERSONA,
        date="2026-06-09",
        narrative="昨天最挂心的是那道没解出来的题。",
        written_at="2026-06-10T05:00:00+08:00",
    )
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert "昨天最挂心的是那道没解出来的题。" in _FakeAgent.last_prompt_vars()["day_page"]


@pytest.mark.asyncio
async def test_day_page_to_prompt_vars_even_without_snapshot(patched, monkeypatch):
    """没有 LifeState（首轮）也照常走读口 → 注入 prompt_vars——注入只依赖页、不依赖快照 / marker。"""
    patched["snapshot"] = None
    patched["day_page"] = DayPage(
        lane=_LANE,
        persona_id=_PERSONA,
        date="2026-06-09",
        narrative="昨天最挂心的是那道没解出来的题。",
        written_at="2026-06-09T23:40:00+08:00",
    )
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert "昨天最挂心的是那道没解出来的题。" in _FakeAgent.last_prompt_vars()["day_page"]
    assert patched["page_reads"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "before_date": "2026-06-10"}
    ]


@pytest.mark.asyncio
async def test_day_page_empty_string_when_none_earlier(patched, monkeypatch):
    """没有更早的页（只有今天凌晨的短页 / 完全无页，读口都返回 None）→ prompt_vars[day_page]
    是空字符串、不补占位（诚实的真空，与现状「无页」路径同行为）。"""
    patched["snapshot"] = _snapshot(
        activity_type="study", day_reviewed_date="2026-06-10"
    )
    patched["day_page"] = None
    _FakeAgent.install(monkeypatch)

    await _wake()

    assert _FakeAgent.last_prompt_vars()["day_page"] == "", "无更早页时 day_page 是空字符串"
    assert "上一页" not in _FakeAgent.last_stimulus()


@pytest.mark.asyncio
async def test_day_page_read_failure_does_not_kill_round(
    patched, monkeypatch, caplog
):
    """读页抛错 → 绝不杀 life 轮：本轮照常 run + 收口，prompt_vars[day_page] 空字符串、
    warning 留痕（注入是上下文增强，失败只 log）。"""
    patched["snapshot"] = _snapshot(activity_type="study", day_reviewed_date=None)

    async def boom_read_day_page_before(*, lane, persona_id, before_date):
        raise RuntimeError("pg down while reading day page")

    monkeypatch.setattr(lw, "read_day_page_before", boom_read_day_page_before)
    _FakeAgent.install(monkeypatch)

    with caplog.at_level("WARNING"):
        await _wake()  # 不抛

    assert _FakeAgent.instances and _FakeAgent.instances[-1].run_calls, (
        "读页失败本轮必须照常跑"
    )
    assert _FakeAgent.last_prompt_vars()["day_page"] == "", "失败时 day_page 是空字符串"
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
