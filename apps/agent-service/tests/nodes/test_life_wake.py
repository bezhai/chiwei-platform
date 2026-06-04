"""life_wake_node — 三姐妹同构 life 节点 (Task 3).

被 EventArrived 攒批唤醒后她做一轮：读自己 LifeState（主观快照）+ 读自己信箱
未读 event → LLM 想一轮做主观解读 → 更新 LifeState → 该 emit 意图就 raise_intent
→ 标已读（只标本轮读到的 event_id）。

这些是节点逻辑测试，LLM 用 mock —— 验证编排正确性，不是验证 LLM 想得对。
最致命的几条（spec 钉死）：

  * **信息差命门**：一轮的输入 = 她自己的 LifeState + 她自己信箱未读 event，
    绝不含 WorldState 全局快照。一旦全局真相漏进她上下文她就全知了。
  * **无 state_end_at / 不自排闹钟**：她脑子里没有"做到几点"。被 event 推、
    不自己定时唤醒自己。
  * **大状态进行中被推醒重想**：她处在一个状态里，信箱进一条指向她的 event，
    被唤醒会读未读 + 重想（更新 LifeState），不干等到原状态"结束"——这是修复
    旧卡死的核心。
  * **标已读只标本轮**：传给 mark_events_read 的就是本轮实际读到的那批 event_id。
"""

from __future__ import annotations

import asyncio

import fakeredis.aioredis
import pytest

import app.nodes.life_wake as lw
from app.domain.life_state import LifeState
from app.domain.world_events import (
    EVENT_KIND_AMBIENT,
    EVENT_KIND_EXTERNAL,
    EventArrived,
    EventEnvelope,
)
from app.runtime.debounce import DebounceReschedule


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.aioredis.FakeRedis:
    """Swap ``app.infra.redis._redis`` with an in-memory FakeRedis.

    Autouse: ``life_wake_node`` now takes a ``(lane, persona)`` single-flight
    lock at the start of every round (必改 2), so *every* test here needs a
    redis. ``get_redis()`` short-circuits when ``_redis`` is non-None, so the
    SETNX + Lua release run against a real (if in-memory) interpreter —
    concurrency contention in the single-flight test is genuine.
    """
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    return fake


def _envelope(event_id, summary, *, kind=EVENT_KIND_AMBIENT, occurred_at="2026-06-03T12:30:00Z"):
    return EventEnvelope(
        lane="coe-t3",
        persona_id="akao",
        event_id=event_id,
        kind=kind,
        source="world",
        room_id="",
        summary=summary,
        occurred_at=occurred_at,
    )


class _StubThink:
    """记录被喂进 LLM 的 prompt_vars，回放一个固定决策。"""

    def __init__(self, decision):
        self.decision = decision
        self.captured_vars = None

    async def __call__(self, *, persona_id, snapshot, unread, prompt_vars):
        self.captured_vars = prompt_vars
        return self.decision


@pytest.fixture
def patched(monkeypatch):
    """把节点的所有 IO 依赖换成可观测的 fake，LLM 思考换成 stub。"""

    state = {
        "snapshot": None,          # find_life_state 返回
        "unread": [],              # list_unread_events 返回
        "saved": [],               # save_life_state 收到的
        "marked": [],              # mark_events_read 收到的 event_ids
        "intents": [],             # raise_intent 收到的
    }

    async def fake_find(*, lane, persona_id):
        return state["snapshot"]

    async def fake_unread(*, lane, persona_id):
        return list(state["unread"])

    async def fake_save(**kwargs):
        state["saved"].append(kwargs)

    async def fake_mark(*, lane, persona_id, event_ids):
        state["marked"].append(event_ids)

    async def fake_intent(*, lane, intent_id, persona_id, summary, occurred_at):
        state["intents"].append(
            {"intent_id": intent_id, "persona_id": persona_id, "summary": summary}
        )

    async def fake_load_persona(persona_id):
        from app.memory._persona import PersonaContext

        return PersonaContext(
            persona_id=persona_id,
            display_name=persona_id,
            persona_lite=f"{persona_id} 的人设",
        )

    monkeypatch.setattr(lw, "find_life_state", fake_find)
    monkeypatch.setattr(lw, "list_unread_events", fake_unread)
    monkeypatch.setattr(lw, "save_life_state", fake_save)
    monkeypatch.setattr(lw, "mark_events_read", fake_mark)
    monkeypatch.setattr(lw, "raise_intent", fake_intent)
    monkeypatch.setattr(lw, "load_persona", fake_load_persona)
    return state


def _decision(current_state="发呆", response_mood="平静", activity_type="idle", intent_summary=None):
    return lw.LifeDecision(
        current_state=current_state,
        response_mood=response_mood,
        activity_type=activity_type,
        intent_summary=intent_summary,
    )


@pytest.mark.asyncio
async def test_wake_reads_unread_thinks_saves_marks(patched, monkeypatch):
    """完整一轮：读未读 → 想 → 存新快照 → 标已读（只标本轮读到的）。"""
    patched["unread"] = [_envelope("e1", "水壶在响"), _envelope("e2", "千凪在厨房")]
    think = _StubThink(_decision(current_state="起身去厨房", response_mood="迷糊"))
    monkeypatch.setattr(lw, "_think", think)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    # 存了新快照
    assert len(patched["saved"]) == 1
    saved = patched["saved"][0]
    assert saved["lane"] == "coe-t3"
    assert saved["persona_id"] == "akao"
    assert saved["current_state"] == "起身去厨房"
    assert saved["response_mood"] == "迷糊"
    # 只标本轮实际读到的那批 event_id
    assert patched["marked"] == [["e1", "e2"]]


@pytest.mark.asyncio
async def test_input_excludes_world_state(patched, monkeypatch):
    """信息差命门：喂给 LLM 的输入只含她自己的快照 + 自己信箱未读，绝不含 WorldState。"""
    patched["snapshot"] = LifeState(
        lane="coe-t3", persona_id="akao",
        current_state="睡觉", response_mood="困", activity_type="sleep",
        observed_at="2026-06-03T08:00:00Z",
    )
    patched["unread"] = [_envelope("e1", "晌午的光很亮")]
    think = _StubThink(_decision())
    monkeypatch.setattr(lw, "_think", think)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    pv = think.captured_vars
    assert pv is not None
    # 不含任何 world 全局快照的痕迹
    blob = repr(pv).lower()
    assert "worldstate" not in blob
    assert "world_state" not in blob
    # 输入确实只是她自己的快照字段 + 她自己信箱的 summary
    assert "睡觉" in repr(pv)
    assert "晌午的光很亮" in repr(pv)


@pytest.mark.asyncio
async def test_decision_carries_no_state_end_at(patched, monkeypatch):
    """无 state_end_at：LLM 决策结构里压根没有"做到几点"这个字段。"""
    assert "state_end_at" not in lw.LifeDecision.model_fields
    assert "skip_until" not in lw.LifeDecision.model_fields


@pytest.mark.asyncio
async def test_no_self_alarm_scheduled(patched, monkeypatch):
    """不自排闹钟：一轮里绝不 emit_delayed / emit_at 给自己定时唤醒。"""
    patched["unread"] = [_envelope("e1", "在看书")]
    monkeypatch.setattr(lw, "_think", _StubThink(_decision(current_state="看书")))

    called = {"delayed": 0, "at": 0}

    async def boom_delayed(*a, **k):
        called["delayed"] += 1

    async def boom_at(*a, **k):
        called["at"] += 1

    # 节点模块即便能 import 到这些原语，也绝不能调它们给自己排闹钟
    monkeypatch.setattr(lw, "emit_delayed", boom_delayed, raising=False)
    monkeypatch.setattr(lw, "emit_at", boom_at, raising=False)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert called == {"delayed": 0, "at": 0}


@pytest.mark.asyncio
async def test_big_state_interrupted_and_rethinks(patched, monkeypatch):
    """修复旧卡死的核心：处在一个大状态里，进一条指向她的 event 能把她推醒重想。"""
    # 她正"在上课"——旧设计会锁死干等到 state_end_at
    patched["snapshot"] = LifeState(
        lane="coe-t3", persona_id="akao",
        current_state="在上课", response_mood="专注", activity_type="study",
        observed_at="2026-06-03T08:05:00Z",
    )
    patched["unread"] = [_envelope("e9", "千凪在门口喊你")]
    think = _StubThink(_decision(current_state="被姐姐喊、抬头应一声", response_mood="无奈"))
    monkeypatch.setattr(lw, "_think", think)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    # 她读到了那条打断的 event（旧快照"在上课"被推醒重想，不干等）
    assert "千凪在门口喊你" in repr(think.captured_vars)
    # 重想后状态变了
    assert patched["saved"][0]["current_state"] == "被姐姐喊、抬头应一声"
    assert patched["marked"] == [["e9"]]


@pytest.mark.asyncio
async def test_digests_external_message_event(patched, monkeypatch):
    """消化外部消息：信箱里有 kind=external（刚和用户聊过）的 event，她能读到。"""
    patched["unread"] = [
        _envelope("ex1", "刚和原智鸿聊了几句", kind=EVENT_KIND_EXTERNAL),
    ]
    think = _StubThink(_decision())
    monkeypatch.setattr(lw, "_think", think)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert "刚和原智鸿聊了几句" in repr(think.captured_vars)
    assert patched["marked"] == [["ex1"]]


@pytest.mark.asyncio
async def test_raises_intent_when_decided(patched, monkeypatch):
    """她想完起了个意图 → raise_intent 回灌唤醒 world。"""
    patched["unread"] = [_envelope("e1", "天亮了")]
    think = _StubThink(_decision(intent_summary="起床去厨房做早饭"))
    monkeypatch.setattr(lw, "_think", think)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(patched["intents"]) == 1
    assert patched["intents"][0]["persona_id"] == "akao"
    assert patched["intents"][0]["summary"] == "起床去厨房做早饭"


@pytest.mark.asyncio
async def test_no_intent_when_none(patched, monkeypatch):
    """没起意图就不回灌 world（她只是默默换了个状态）。"""
    patched["unread"] = [_envelope("e1", "外面在下雨")]
    monkeypatch.setattr(lw, "_think", _StubThink(_decision(intent_summary=None)))

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert patched["intents"] == []


@pytest.mark.asyncio
async def test_no_unread_is_noop(patched, monkeypatch):
    """信箱没未读（空唤醒）：不烧 LLM、不写快照、不标已读。"""
    patched["unread"] = []
    think = _StubThink(_decision())
    monkeypatch.setattr(lw, "_think", think)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert think.captured_vars is None  # LLM 没被调
    assert patched["saved"] == []
    assert patched["marked"] == []


@pytest.mark.asyncio
async def test_concurrent_second_round_reschedules_no_overwrite_no_loss(
    patched, fake_redis, monkeypatch
):
    """必改 2 复现：同 (lane,persona) 两轮并发，第二轮单飞落空 → DebounceReschedule。

    旧 bug：life 一轮 LLM 跑几十秒 > debounce 窗口（5s），期间来新 event 会 fire
    第二轮 life_wake **并发**。两轮并发会互相覆盖 LifeState、把 event 静默标已读
    丢掉，绕回原痛点。

    复现：让第一轮在 LLM 思考处阻塞（持锁不释放），同时启动第二轮。第二轮拿不到
    单飞锁 → raise DebounceReschedule（交给 debounce handler CAS 重排，稍后再试），
    且**不写快照、不标已读、不起意图**——既不覆盖第一轮，也不静默吞掉 event。
    """
    patched["unread"] = [_envelope("e1", "水壶在响"), _envelope("e2", "千凪在厨房")]

    round1_in_think = asyncio.Event()
    release_round1 = asyncio.Event()

    class _BlockingThink:
        """第一轮在 LLM 思考处阻塞，模拟 LLM 跑几十秒未返回——持锁不释放。

        只有第一次调用阻塞；万一第二轮（无锁的旧 bug 下）也跑到 _think，让它
        立即返回，不死锁——这样旧 bug 表现为"第二轮也写了快照/标了已读"的干净
        断言失败（红），而不是 hang。
        """

        def __init__(self):
            self.calls = 0

        async def __call__(self, *, persona_id, snapshot, unread, prompt_vars):
            self.calls += 1
            if self.calls == 1:
                round1_in_think.set()
                await release_round1.wait()
                return _decision(current_state="第一轮慢慢想出的状态")
            return _decision(current_state="第二轮并发想出的状态（不该发生）")

    think = _BlockingThink()
    monkeypatch.setattr(lw, "_think", think)

    arrived = EventArrived(lane="coe-t3", persona_id="akao")

    # 第一轮：开始跑，会卡在 _think 里（持锁）
    round1 = asyncio.create_task(lw.life_wake_node(arrived))
    await round1_in_think.wait()

    # 第二轮：同 (lane,persona) 此刻并发进来 —— 单飞锁被第一轮持有，必须落空
    with pytest.raises(DebounceReschedule) as ei:
        await lw.life_wake_node(arrived)

    # raise 的是这一批 EventArrived（让 handler 用它 CAS 重排）
    assert ei.value.data is arrived

    # 第二轮没烧 LLM（只第一轮调了 _think），没覆盖第一轮快照，没标已读、没起意图
    assert think.calls == 1, "第二轮不该并发再跑一遍 LLM"
    assert patched["saved"] == [], "第二轮被 reschedule 时绝不能写 LifeState（避免覆盖）"
    assert patched["marked"] == [], "第二轮绝不能标已读（避免静默吞掉 event）"
    assert patched["intents"] == []

    # 放第一轮跑完：它正常写自己的快照 + 标自己读到的那批
    release_round1.set()
    await round1

    assert [s["current_state"] for s in patched["saved"]] == ["第一轮慢慢想出的状态"]
    assert patched["marked"] == [["e1", "e2"]]


@pytest.mark.asyncio
async def test_single_flight_lock_released_allows_next_round(
    patched, fake_redis, monkeypatch
):
    """单飞锁跑完即释放：上一轮结束后，下一轮能正常拿到锁、不被永久卡住。"""
    patched["unread"] = [_envelope("e1", "天亮了")]
    monkeypatch.setattr(lw, "_think", _StubThink(_decision(current_state="第一轮")))
    arrived = EventArrived(lane="coe-t3", persona_id="akao")

    await lw.life_wake_node(arrived)  # 第一轮跑完、释放锁

    patched["unread"] = [_envelope("e2", "中午了")]
    monkeypatch.setattr(lw, "_think", _StubThink(_decision(current_state="第二轮")))

    # 第二轮（串行、上一轮已释放锁）能正常拿锁、不抛 DebounceReschedule
    await lw.life_wake_node(arrived)

    assert [s["current_state"] for s in patched["saved"]] == ["第一轮", "第二轮"]
    assert patched["marked"] == [["e1"], ["e2"]]
