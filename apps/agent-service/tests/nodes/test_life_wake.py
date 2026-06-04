"""life_wake_node — 三姐妹同构 life 节点 (Task 3, agent 工具循环).

被 EventArrived 攒批唤醒后她跑一个 ReAct 循环：读自己 LifeState（主观快照）+ 读
自己信箱未读 event → 喂进 ``Agent(...).run`` 跑工具循环（连续调 update_life_state /
raise_intent 行动）→ 收口标已读（只标本轮读到的 event_id）。输出来自工具调用，不
再填一张 LifeDecision 表。

这些是节点编排测试：``Agent.run`` 用 fake 模拟模型在循环里调工具，验证编排正确性，
不验证 LLM 想得对。最致命的几条（spec 钉死）：

  * **信息差命门**：一轮的输入 = 她自己的 LifeState + 她自己信箱未读 event，
    绝不含 WorldState 全局快照。本模块绝不 import / 读 world 快照。
  * **空信箱 early-return**：信箱没未读就不烧模型、不建工具、不写、不标已读。
  * **single_flight 锁**：同 (lane,persona) 两轮并发，第二轮拿不到锁 →
    DebounceReschedule（不覆盖、不静默吞 event）。
  * **收口标已读只标本轮**：传给 mark_events_read 的就是本轮实际读到的那批
    event_id，即使一次 update 都没调也照常标已读（看了但没改状态，正常）。
  * **max_retries=1 + session_id**：run 必须传 max_retries=1（关掉整轮重放、不
    重放 durable 工具）和按 (lane, persona, 今天) 派生的 session_id。
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

    Autouse: ``life_wake_node`` 每轮开头按 (lane, persona) 取一把 single-flight
    锁（必改 2），所以这里每个测试都需要 redis。``get_redis()`` 在 ``_redis`` 非
    None 时短路，SETNX + Lua 释放跑在真（in-memory）解释器上 —— single-flight
    测试的并发竞争是真实的。
    """
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    return fake


def _envelope(
    event_id, summary, *, kind=EVENT_KIND_AMBIENT, occurred_at="2026-06-03T12:30:00Z"
):
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


class _FakeAgent:
    """模拟 ``Agent(cfg, tools=...).run(...)``：记录构造 + run 参数，回放脚本。

    ``script`` 是一个 ``async def(tools) -> None`` 回调，模拟模型在循环里调哪些
    工具（直接 invoke 传进来的 Tool），从而触发 handler 副作用。默认什么工具都
    不调（模拟模型看了但没改状态）。
    """

    # 记录跨实例的所有构造 / run（节点每轮 new 一个 Agent）。
    instances: list = []

    def __init__(self, cfg, *, tools=None, **kwargs):
        self.cfg = cfg
        self.tools = tools or []
        self.run_calls: list[dict] = []
        _FakeAgent.instances.append(self)

    @classmethod
    def reset(cls):
        cls.instances = []

    @classmethod
    def install(cls, monkeypatch, script=None):
        cls.reset()
        cls._script = staticmethod(script) if script else None
        monkeypatch.setattr(lw, "Agent", cls)
        return cls

    async def run(self, messages, *, prompt_vars=None, context=None, max_retries=None):
        self.run_calls.append(
            {
                "messages": messages,
                "prompt_vars": prompt_vars,
                "context": context,
                "max_retries": max_retries,
            }
        )
        script = getattr(_FakeAgent, "_script", None)
        if script is not None:
            await script(self.tools)
        from app.agent.neutral import Message, Role

        return Message(role=Role.ASSISTANT, content="ok")

    # 便于断言：节点这一轮唯一那个 Agent 的 run_call。
    @classmethod
    def last_run(cls):
        assert cls.instances, "Agent 从没被构造（run 没被调）"
        last = cls.instances[-1]
        assert last.run_calls, "Agent 被构造但 run 没被调"
        return last.run_calls[-1]


@pytest.fixture
def patched(monkeypatch):
    """把节点的 IO 依赖换成可观测 fake；工具的 handler 也打桩。

    工具是 build_life_tools 造出来的真 Tool，但底下的 save_life_state /
    raise_intent handler 在 life_tools 模块里被打桩成记录副作用。
    """
    import app.nodes.life_tools as lt

    state = {
        "snapshot": None,  # find_life_state 返回
        "unread": [],  # list_unread_events 返回
        "saved": [],  # save_life_state 收到的
        "marked": [],  # mark_events_read 收到的 event_ids
        "intents": [],  # raise_intent 收到的
    }

    async def fake_find(*, lane, persona_id):
        return state["snapshot"]

    async def fake_unread(*, lane, persona_id):
        return list(state["unread"])

    async def fake_mark(*, lane, persona_id, event_ids):
        state["marked"].append(event_ids)

    async def fake_save(**kwargs):
        state["saved"].append(kwargs)

    async def fake_intent(**kwargs):
        state["intents"].append(kwargs)

    async def fake_load_persona(persona_id):
        from app.memory._persona import PersonaContext

        return PersonaContext(
            persona_id=persona_id,
            display_name=persona_id,
            persona_lite=f"{persona_id} 的人设",
        )

    monkeypatch.setattr(lw, "find_life_state", fake_find)
    monkeypatch.setattr(lw, "list_unread_events", fake_unread)
    monkeypatch.setattr(lw, "mark_events_read", fake_mark)
    monkeypatch.setattr(lw, "load_persona", fake_load_persona)
    # 工具底下的 durable handler：在 life_tools 模块里打桩。
    monkeypatch.setattr(lt, "save_life_state", fake_save)
    monkeypatch.setattr(lt, "raise_intent", fake_intent)
    return state


# 脚本：模型在循环里"调一次 update_life_state"。
def _script_update(current_state="起身去厨房", response_mood="迷糊", activity_type="move"):
    async def _run(tools):
        by_name = {t.name: t for t in tools}
        await by_name["update_life_state"].invoke(
            {
                "current_state": current_state,
                "response_mood": response_mood,
                "activity_type": activity_type,
            }
        )

    return _run


def _script_update_then_intent(summary="去厨房煮咖啡"):
    async def _run(tools):
        by_name = {t.name: t for t in tools}
        await by_name["update_life_state"].invoke(
            {"current_state": "醒了", "response_mood": "迷糊", "activity_type": "move"}
        )
        await by_name["raise_intent"].invoke({"summary": summary})

    return _run


@pytest.mark.asyncio
async def test_wake_runs_loop_updates_state_marks_read(patched, monkeypatch):
    """完整一轮：读未读 → 跑循环（模型调 update_life_state）→ 收口标已读（只标本轮）。"""
    patched["unread"] = [_envelope("e1", "水壶在响"), _envelope("e2", "千凪在厨房")]
    _FakeAgent.install(
        monkeypatch, script=_script_update(current_state="起身去厨房", response_mood="迷糊")
    )

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(patched["saved"]) == 1
    saved = patched["saved"][0]
    assert saved["lane"] == "coe-t3"
    assert saved["persona_id"] == "akao"
    assert saved["current_state"] == "起身去厨房"
    assert saved["response_mood"] == "迷糊"
    # 只标本轮实际读到的那批 event_id
    assert patched["marked"] == [["e1", "e2"]]


@pytest.mark.asyncio
async def test_zero_update_still_marks_read(patched, monkeypatch):
    """0 次 update 也照常标已读（她看了但没改状态，正常 —— spec 决策 2）。"""
    patched["unread"] = [_envelope("e1", "外面在下雨")]
    # 脚本什么工具都不调
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert patched["saved"] == []  # 没改状态
    assert patched["marked"] == [["e1"]]  # 但照常标已读


@pytest.mark.asyncio
async def test_run_gets_max_retries_one_and_session_id(patched, monkeypatch):
    """run 必须传 max_retries=1（关整轮重放）+ 按 (lane, persona, 今天) 的 session_id。"""
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    # 钉死 now，使 session_id 的日期可断言
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 3, 12, 30, tzinfo=tz)

    monkeypatch.setattr(lw, "datetime", _FixedDateTime)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    call = _FakeAgent.last_run()
    assert call["max_retries"] == 1, "run 必须传 max_retries=1，否则整轮重放重放 durable 工具"
    ctx = call["context"]
    assert ctx is not None
    # session 按 (lane, persona, 今天 YYYY-MM-DD) 派生
    from app.agent.trace import make_session_id

    assert ctx.session_id == make_session_id("coe-t3", "akao", "2026-06-03")


@pytest.mark.asyncio
async def test_context_excludes_world_state(patched, monkeypatch):
    """信息差命门：喂给 run 的 prompt_vars 只含她自己快照 + 自己信箱未读，绝不含 WorldState。"""
    patched["snapshot"] = LifeState(
        lane="coe-t3",
        persona_id="akao",
        current_state="睡觉",
        response_mood="困",
        activity_type="sleep",
        observed_at="2026-06-03T08:00:00Z",
    )
    patched["unread"] = [_envelope("e1", "晌午的光很亮")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    pv = _FakeAgent.last_run()["prompt_vars"]
    assert pv is not None
    blob = repr(pv).lower()
    assert "worldstate" not in blob
    assert "world_state" not in blob
    # 输入确实只是她自己的快照字段 + 她自己信箱的 summary
    assert "睡觉" in repr(pv)
    assert "晌午的光很亮" in repr(pv)


@pytest.mark.asyncio
async def test_no_world_snapshot_import():
    """信息差结构保证：life_wake 模块绝不 import / 读 world 快照（代码层面）。

    扫 AST 的 import 语句（不是 docstring 文本）—— docstring 里解释"绝不读
    WorldState"是合法的，真正的命门是模块没 import / 引用任何 world 快照符号。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(lw))
    imported_modules: list[str] = []
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.ImportFrom) and stmt.module:
            imported_modules.append(stmt.module)
        elif isinstance(stmt, ast.Import):
            imported_modules.extend(a.name for a in stmt.names)

    for mod in imported_modules:
        assert not mod.startswith("app.world"), f"life_wake 不该 import world 模块: {mod}"

    # 也不该按名字引用任何 world 快照符号（在非注释/非 docstring 的标识符里）
    referenced_names = {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    } | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    for forbidden in ("WorldState", "read_presence", "read_world_state"):
        assert forbidden not in referenced_names, f"life_wake 引用了 world 符号 {forbidden}"


@pytest.mark.asyncio
async def test_no_self_alarm_scheduled(patched, monkeypatch):
    """不自排闹钟：一轮里绝不 emit_delayed / emit_at 给自己定时唤醒。"""
    patched["unread"] = [_envelope("e1", "在看书")]
    _FakeAgent.install(monkeypatch, script=_script_update(current_state="看书"))

    called = {"delayed": 0, "at": 0}

    async def boom_delayed(*a, **k):
        called["delayed"] += 1

    async def boom_at(*a, **k):
        called["at"] += 1

    monkeypatch.setattr(lw, "emit_delayed", boom_delayed, raising=False)
    monkeypatch.setattr(lw, "emit_at", boom_at, raising=False)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert called == {"delayed": 0, "at": 0}


@pytest.mark.asyncio
async def test_digests_external_message_event(patched, monkeypatch):
    """消化外部消息：信箱里有 kind=external（刚和用户聊过）的 event，她能读到。"""
    patched["unread"] = [
        _envelope("ex1", "刚和原智鸿聊了几句", kind=EVENT_KIND_EXTERNAL),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    pv = _FakeAgent.last_run()["prompt_vars"]
    assert "刚和原智鸿聊了几句" in repr(pv)
    assert patched["marked"] == [["ex1"]]


@pytest.mark.asyncio
async def test_raises_intent_when_model_calls_it(patched, monkeypatch):
    """模型在循环里调 raise_intent → 回灌唤醒 world，intent_id 由本轮 event_ids 派生。"""
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(
        monkeypatch, script=_script_update_then_intent(summary="起床去厨房做早饭")
    )

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(patched["intents"]) == 1
    assert patched["intents"][0]["persona_id"] == "akao"
    assert patched["intents"][0]["summary"] == "起床去厨房做早饭"
    assert patched["intents"][0]["lane"] == "coe-t3"
    # intent_id 必须是基于本轮 event_ids 的确定派生（整轮重放幂等）
    import uuid

    seed = "coe-t3:akao:e1"
    expected = str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))
    assert patched["intents"][0]["intent_id"] == expected


def _script_intent_twice(summary1="去厨房煮咖啡", summary2="顺便给千凪带一杯"):
    async def _run(tools):
        by_name = {t.name: t for t in tools}
        await by_name["raise_intent"].invoke({"summary": summary1})
        await by_name["raise_intent"].invoke({"summary": summary2})

    return _run


@pytest.mark.asyncio
async def test_two_raise_intent_in_one_round_only_first_emits(patched, monkeypatch):
    """一轮里模型调两次 raise_intent：只有第一个真正起意图（emit 一个 IntentRaised）。

    intent_id 由本轮 event_ids 派生，同一轮两次共用同一个 intent_id —— 第二个会被
    durable 去重层静默吞掉、意图无声丢失。第一刀：本轮已起过意图后第二次不再落
    handler（绝不静默吞，工具层 log + 喂回提示）。
    """
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(
        monkeypatch,
        script=_script_intent_twice(summary1="起床去厨房", summary2="再去叫醒千凪"),
    )

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    # 一轮只起一个意图：只有第一个落到 handler
    assert len(patched["intents"]) == 1
    assert patched["intents"][0]["summary"] == "起床去厨房"
    assert patched["intents"][0]["persona_id"] == "akao"
    assert patched["intents"][0]["lane"] == "coe-t3"
    # 收口照常标已读
    assert patched["marked"] == [["e1"]]


@pytest.mark.asyncio
async def test_no_intent_when_model_doesnt_call_it(patched, monkeypatch):
    """没调 raise_intent 就不回灌 world（她只是默默换了个状态）。"""
    patched["unread"] = [_envelope("e1", "外面在下雨")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert patched["intents"] == []


@pytest.mark.asyncio
async def test_no_unread_is_noop(patched, monkeypatch):
    """信箱没未读（空唤醒）：不烧模型（不建 Agent）、不写、不标已读。"""
    patched["unread"] = []
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert _FakeAgent.instances == []  # Agent 从没被构造（模型没被烧）
    assert patched["saved"] == []
    assert patched["marked"] == []


@pytest.mark.asyncio
async def test_inbox_cap_truncates_and_logs(patched, monkeypatch, caplog):
    """安全阀（spec 决策 4）：一轮读 inbox 设上限，积压过多分批 / 截断并 log（不静默）。

    只读上限那批喂给模型 + 只标那批已读；剩下的留未读、下轮再处理（不被吞）。
    """
    cap = lw._LIFE_INBOX_MAX
    overflow = cap + 3
    patched["unread"] = [_envelope(f"e{i}", f"动静{i}") for i in range(overflow)]
    _FakeAgent.install(monkeypatch, script=None)

    import logging

    with caplog.at_level(logging.WARNING):
        await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    # 只标了上限那批（前 cap 个），剩下的没标、仍未读
    assert len(patched["marked"]) == 1
    assert patched["marked"][0] == [f"e{i}" for i in range(cap)]
    # 截断要 log，不静默
    assert any("inbox" in r.message.lower() or "积压" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_recursion_limit_is_generous(monkeypatch):
    """recursion_limit 给够（≥10）：让她在一轮里能连续调多次工具，不被 6 卡住。"""
    assert lw._LIFE_WAKE_CFG.recursion_limit >= 10


@pytest.mark.asyncio
async def test_concurrent_second_round_reschedules_no_overwrite_no_loss(
    patched, fake_redis, monkeypatch
):
    """必改 2：同 (lane,persona) 两轮并发，第二轮单飞落空 → DebounceReschedule。

    第一轮在循环里阻塞（持锁不释放），第二轮拿不到单飞锁 → raise
    DebounceReschedule，且不写快照、不标已读、不起意图（既不覆盖第一轮，也不
    静默吞 event）。
    """
    patched["unread"] = [_envelope("e1", "水壶在响"), _envelope("e2", "千凪在厨房")]

    round1_in_run = asyncio.Event()
    release_round1 = asyncio.Event()
    run_count = {"n": 0}

    async def _blocking_script(tools):
        run_count["n"] += 1
        if run_count["n"] == 1:
            round1_in_run.set()
            await release_round1.wait()
            by_name = {t.name: t for t in tools}
            await by_name["update_life_state"].invoke(
                {
                    "current_state": "第一轮慢慢想出的状态",
                    "response_mood": "平静",
                    "activity_type": "idle",
                }
            )
        else:
            by_name = {t.name: t for t in tools}
            await by_name["update_life_state"].invoke(
                {
                    "current_state": "第二轮并发（不该发生）",
                    "response_mood": "x",
                    "activity_type": "y",
                }
            )

    _FakeAgent.install(monkeypatch, script=_blocking_script)

    arrived = EventArrived(lane="coe-t3", persona_id="akao")

    round1 = asyncio.create_task(lw.life_wake_node(arrived))
    await round1_in_run.wait()

    with pytest.raises(DebounceReschedule) as ei:
        await lw.life_wake_node(arrived)

    assert ei.value.data is arrived
    assert run_count["n"] == 1, "第二轮不该并发再跑一遍循环"
    assert patched["saved"] == [], "第二轮被 reschedule 时绝不能写 LifeState（避免覆盖）"
    assert patched["marked"] == [], "第二轮绝不能标已读（避免静默吞掉 event）"
    assert patched["intents"] == []

    release_round1.set()
    await round1

    assert [s["current_state"] for s in patched["saved"]] == ["第一轮慢慢想出的状态"]
    assert patched["marked"] == [["e1", "e2"]]


@pytest.mark.asyncio
async def test_single_flight_lock_released_allows_next_round(
    patched, fake_redis, monkeypatch
):
    """单飞锁跑完即释放：上一轮结束后，下一轮能正常拿锁、不被永久卡住。"""
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update(current_state="第一轮"))
    arrived = EventArrived(lane="coe-t3", persona_id="akao")

    await lw.life_wake_node(arrived)

    patched["unread"] = [_envelope("e2", "中午了")]
    _FakeAgent.install(monkeypatch, script=_script_update(current_state="第二轮"))

    await lw.life_wake_node(arrived)

    assert [s["current_state"] for s in patched["saved"]] == ["第一轮", "第二轮"]
    assert patched["marked"] == [["e1"], ["e2"]]
