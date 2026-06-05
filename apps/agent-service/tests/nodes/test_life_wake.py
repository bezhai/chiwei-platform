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

    async def run(
        self, messages, *, prompt_vars=None, context=None, session_id=None,
        max_retries=None,
    ):
        self.run_calls.append(
            {
                "messages": messages,
                "prompt_vars": prompt_vars,
                "context": context,
                "session_id": session_id,
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
        "transcript": [],  # load_session 探测返回（空=冷启）
    }

    async def fake_find(*, lane, persona_id):
        return state["snapshot"]

    async def fake_unread(*, lane, persona_id):
        return list(state["unread"])

    async def fake_load_session(session_id, **kwargs):
        return list(state["transcript"])

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
    monkeypatch.setattr(lw, "load_session", fake_load_session)
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
    """run 必须传 max_retries=1（关整轮重放）+ 按 (lane, persona, 今天) 的 session_id。

    session_id 既塞进 context（langfuse 归类一致），也**显式传给 run**（task1 的
    run 见到显式 session_id 才真从 Redis 读历史续接、跑完写回；只塞 context 不读写）。
    """
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    # 钉死 now，使 session_id 的日期可断言。now 现在走 cst_time.now_cst()（CST
    # aware），日期由 CST 钟点算，所以这里钉的就是 CST 时区的 now。
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 3, 12, 30, tzinfo=tz)

    monkeypatch.setattr(lw.cst_time, "datetime", _FixedDateTime)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    call = _FakeAgent.last_run()
    assert call["max_retries"] == 1, "run 必须传 max_retries=1，否则整轮重放重放 durable 工具"
    ctx = call["context"]
    assert ctx is not None
    # session 按 (lane, persona, 今天 YYYY-MM-DD) 派生
    from app.agent.trace import make_session_id

    expected = make_session_id("coe-t3", "akao", "2026-06-03")
    assert ctx.session_id == expected
    # 续接命门：必须**显式**把 session_id 传给 run（不只塞 context）—— task1 的 run
    # 只在收到显式 session_id 时才读 Redis 历史续接 + 写回。
    assert call["session_id"] == expected, (
        "life 必须显式把 session_id 传给 run，否则不续接（只塞 context 不读写历史）"
    )


# ---------------------------------------------------------------------------
# CST 时间归一（阶段 0 Task 1）—— life 这一轮产 / 显示的所有时间都走 CST 口径。
# 只测时间值这一层；prompt_vars 键集合 / stimulus 结构是 Task 2，这里不动。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_observed_at_is_cst_aware_iso(patched, monkeypatch):
    """她这一轮写的 observed_at 是 CST aware ISO（含 +08:00），不再 UTC。

    旧 bug：``_TZ = UTC`` → observed_at 写 ``...+00:00``，跟 world 的 CST、chat
    的 Unix 毫秒同框混着喂给 agent、时间窗口比较差 8 小时。改成 CST aware ISO。
    """
    from app.infra import cst_time

    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(patched["saved"]) == 1
    observed_at = patched["saved"][0]["observed_at"]
    assert "+08:00" in observed_at, (
        f"observed_at 必须是 CST aware ISO（含 +08:00），实际 {observed_at!r}"
    )
    assert cst_time.parse(observed_at) is not None


@pytest.mark.asyncio
async def test_intent_occurred_at_is_cst_aware_iso(patched, monkeypatch):
    """intent 的 occurred_at 是 observed_at 的 pass-through → 也跟着 CST aware。"""
    from app.infra import cst_time

    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(
        monkeypatch, script=_script_update_then_intent(summary="去厨房")
    )

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(patched["intents"]) == 1
    occ = patched["intents"][0]["occurred_at"]
    assert "+08:00" in occ, f"intent occurred_at 该 CST aware，实际 {occ!r}"
    assert cst_time.parse(occ) is not None


@pytest.mark.asyncio
async def test_current_time_shows_cst_in_user_stimulus(patched, monkeypatch):
    """"现在几点"作为当轮新感知显示成 CST，且进 USER message（Task 2 挪出 prompt_vars）。

    钉死 now 让 CST 钟点可断言：真实 UTC 12:30 → CST 20:30。Task 2 后 current_time
    不再走 prompt_vars→system，而是当轮新感知拼进 USER stimulus。
    """
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            # 真实 UTC 12:30 这个时刻，按传入 tz 表示
            base = cls(2026, 6, 3, 12, 30, tzinfo=_dt.timezone.utc)
            return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)

    monkeypatch.setattr(lw.cst_time, "datetime", _FixedDateTime)

    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    call = _FakeAgent.last_run()
    pv = call["prompt_vars"]
    assert "current_time" not in pv, "Task 2：current_time 不再走 prompt_vars→system"
    msg_blob = "".join(m.text() for m in call["messages"])
    assert "20:30" in msg_blob, (
        f"当轮几点该显示 CST 钟点并进 USER（UTC 12:30 → CST 20:30），实际 {msg_blob!r}"
    )
    assert "CST" in msg_blob


def _utc_millis(y, mo, d, h, mi, s):
    import datetime as _dt

    return int(
        _dt.datetime(y, mo, d, h, mi, s, tzinfo=_dt.timezone.utc).timestamp() * 1000
    )


@pytest.mark.asyncio
async def test_format_unread_shows_event_time_in_cst(patched, monkeypatch):
    """信箱里 event 的 occurred_at 显示转 CST（兜历史 UTC / Unix 毫秒）。

    UTC 串 ``...12:30:00Z`` 显示成 CST 20:30；Unix 毫秒同理。stimulus 文本结构
    本任务不动（Task 2），只验时间值显示是 CST。
    """
    millis = _utc_millis(2026, 6, 3, 5, 0, 0)  # 真实 UTC 05:00 → CST 13:00
    patched["unread"] = [
        _envelope("e1", "晨光", occurred_at="2026-06-03T12:30:00Z"),
        _envelope("e2", "午后", occurred_at=str(millis)),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # UTC 12:30 → CST 20:30；Unix 05:00 → CST 13:00
    assert "20:30" in msg_blob, "UTC event 时刻该显示成 CST"
    assert "13:00" in msg_blob, "Unix 毫秒 event 时刻该显示成 CST"
    assert "CST" in msg_blob


@pytest.mark.asyncio
async def test_context_excludes_world_state(patched, monkeypatch):
    """信息差命门：喂给 run 的输入（prompt_vars + messages）只含她自己快照 + 自己信箱未读，绝不含 WorldState。

    Task 2 后她此刻的主观快照不再走 prompt_vars；冷启（transcript 空）时作状态恢复段
    进 USER message，信箱里这一轮感知到的 event 也走 USER messages（进 transcript，
    第二轮可 replay）。两处合起来是她这一轮的全部输入，都不该含任何 world 全局快照。
    """
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

    call = _FakeAgent.last_run()
    pv = call["prompt_vars"]
    assert pv is not None
    # 信息差命门：prompt_vars 与 USER messages 两处都绝不含 WorldState 全局符号
    msg_blob = "".join(m.text() for m in call["messages"])
    full_blob = (repr(pv) + msg_blob).lower()
    assert "worldstate" not in full_blob
    assert "world_state" not in full_blob
    # 冷启（transcript 空）→ 她自己的主观快照作恢复段进 USER message
    assert "睡觉" in msg_blob
    # 她信箱里感知到的 event 在 USER messages（→进 transcript→第二轮可 replay）
    assert "晌午的光很亮" in msg_blob


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

    # 感知到的 external event 进 USER messages（→进 transcript→第二轮可 replay）
    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    assert "刚和原智鸿聊了几句" in msg_blob
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
    """单飞锁跑完即释放：上一轮结束后，cd 过后下一轮能正常拿锁、不被永久卡住。

    cd 把"刚跑完的冷却"压在 reschedule 上，所以这里把 cd key 在第一轮后清掉模拟
    cd 已过（cd 的延迟语义由专门的 cd 测试覆盖），验单飞锁本身释放后第二轮能跑。
    """
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update(current_state="第一轮"))
    arrived = EventArrived(lane="coe-t3", persona_id="akao")

    await lw.life_wake_node(arrived)

    # 模拟 cd 已过（删 cd key），验单飞锁释放后第二轮能跑。
    await fake_redis.delete(lw._cd_key("coe-t3", "akao"))

    patched["unread"] = [_envelope("e2", "中午了")]
    _FakeAgent.install(monkeypatch, script=_script_update(current_state="第二轮"))

    await lw.life_wake_node(arrived)

    assert [s["current_state"] for s in patched["saved"]] == ["第一轮", "第二轮"]
    assert patched["marked"] == [["e1"], ["e2"]]


# ---------------------------------------------------------------------------
# cd（一轮跑完后的冷却，降频）—— spec 决策 5 的第三层"延迟+合并不丢事件"。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cd_is_reasonable_and_under_world_gate():
    """cd 时长合理：> 0 且不长于 world 的 60s 唤醒合并闸（life 对齐或略小）。"""
    assert 0 < lw._LIFE_CD_SECONDS <= 60


@pytest.mark.asyncio
async def test_successful_round_sets_cd_key(patched, fake_redis, monkeypatch):
    """一轮成功跑完后落一个 cd key（TTL=cd 秒），把"刚跑完的冷却"记在 redis。"""
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    cd_key = lw._cd_key("coe-t3", "akao")
    assert await fake_redis.get(cd_key) is not None, "成功跑完应落 cd key"
    ttl = await fake_redis.ttl(cd_key)
    assert 0 < ttl <= lw._LIFE_CD_SECONDS, "cd key 必须带 TTL（不能永不过期）"


@pytest.mark.asyncio
async def test_round_in_cd_reschedules_without_running_or_dropping(
    patched, fake_redis, monkeypatch
):
    """cd 内来的 event 不立即醒：raise DebounceReschedule 攒着，不烧模型、不丢 event。

    复用现有 DebounceReschedule 机制——cd 内到达的 event 被推迟到 cd 后，绝不 drop
    （reschedule 把 EventArrived 重排，cd 结束一并醒）。cd 内这一轮：不建 Agent、
    不写快照、不标已读、不起意图。
    """
    arrived = EventArrived(lane="coe-t3", persona_id="akao")
    # 预置 cd key（模拟上一轮刚跑完、还在冷却里）
    await fake_redis.set(lw._cd_key("coe-t3", "akao"), "1", ex=lw._LIFE_CD_SECONDS)

    patched["unread"] = [_envelope("e1", "cd 内来的动静")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    with pytest.raises(DebounceReschedule) as ei:
        await lw.life_wake_node(arrived)

    # 重排的就是这批 EventArrived（不丢）
    assert ei.value.data is arrived
    # cd 内绝不跑：不建 Agent、不写、不标已读、不起意图
    assert _FakeAgent.instances == [], "cd 内不该烧模型（不建 Agent）"
    assert patched["saved"] == []
    assert patched["marked"] == []
    assert patched["intents"] == []


@pytest.mark.asyncio
async def test_after_cd_expires_round_runs_and_consumes_events(
    patched, fake_redis, monkeypatch
):
    """cd 结束后：被重排攒着的 event 一并醒、一并感知、一并标已读（不丢）。

    模拟 cd 已过（删 cd key）后再唤醒——这一轮正常跑、读到 cd 内攒下的 event。
    """
    arrived = EventArrived(lane="coe-t3", persona_id="akao")
    # 先处在 cd 内：第一次唤醒被 reschedule
    await fake_redis.set(lw._cd_key("coe-t3", "akao"), "1", ex=lw._LIFE_CD_SECONDS)
    patched["unread"] = [_envelope("e1", "cd 内攒下的动静")]
    _FakeAgent.install(monkeypatch, script=_script_update())
    with pytest.raises(DebounceReschedule):
        await lw.life_wake_node(arrived)
    assert patched["marked"] == []  # cd 内确实没消费

    # cd 过了（删 key）：重排到点再唤醒 —— 这一轮正常跑、读到攒下的 event
    await fake_redis.delete(lw._cd_key("coe-t3", "akao"))
    _FakeAgent.install(monkeypatch, script=_script_update(current_state="cd 后醒了"))

    await lw.life_wake_node(arrived)

    assert [s["current_state"] for s in patched["saved"]] == ["cd 后醒了"]
    assert patched["marked"] == [["e1"]], "cd 内攒下的 event 在 cd 后被一并消费、标已读"


@pytest.mark.asyncio
async def test_cd_not_set_on_single_flight_conflict(
    patched, fake_redis, monkeypatch
):
    """单飞撞锁被 reschedule 的那轮不落 cd key（它根本没成功跑完一轮）。

    cd 管"刚成功跑完的冷却"，single_flight 管"正在跑"。撞锁的轮没跑完，不该污染
    cd —— 否则真正跑完的那轮反而被这条虚假 cd 卡住。
    """
    patched["unread"] = [_envelope("e1", "水壶在响")]

    round1_in_run = asyncio.Event()
    release_round1 = asyncio.Event()
    run_count = {"n": 0}

    async def _blocking_script(tools):
        run_count["n"] += 1
        round1_in_run.set()
        await release_round1.wait()
        by_name = {t.name: t for t in tools}
        await by_name["update_life_state"].invoke(
            {"current_state": "想完了", "response_mood": "平静", "activity_type": "idle"}
        )

    _FakeAgent.install(monkeypatch, script=_blocking_script)
    arrived = EventArrived(lane="coe-t3", persona_id="akao")

    round1 = asyncio.create_task(lw.life_wake_node(arrived))
    await round1_in_run.wait()

    # 第二轮撞锁 → DebounceReschedule；撞锁的轮不该落 cd key
    with pytest.raises(DebounceReschedule):
        await lw.life_wake_node(arrived)
    # 第一轮还在跑（没收口），此刻 cd key 还不该存在
    assert await fake_redis.get(lw._cd_key("coe-t3", "akao")) is None

    release_round1.set()
    await round1

    # 第一轮成功收口后才落 cd key
    assert await fake_redis.get(lw._cd_key("coe-t3", "akao")) is not None


# ---------------------------------------------------------------------------
# 动态感知进 transcript（session 续接命门）—— life 这一轮感知到的 event 必须进
# 写回 session 的 messages，第二轮 replay 才看得到"上一轮我感知了什么"。
#
# 旧 bug：unread_events 经 prompt_vars 渲染进 langfuse 模板的 **system prompt**，
# system prompt 不进 transcript（core 的 transcript 只存本轮传入 messages + 助手
# + 工具结果）；life 传的 messages 是一句固定文案，于是写回 session 的只有固定
# 文案、不含她感知的 event 原文 —— 第二轮看不到上一轮感知。对比 world：world 把
# 当前 context 拼进 USER message（进 messages → 进 transcript），记忆完整。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_perceived_events_enter_user_stimulus_for_transcript(patched, monkeypatch):
    """life 这一轮感知到的 event 原文必须进传给 run 的 messages（=写回 session 的内容）。

    run 收到的 messages 就是 ``append_session(session_id, [*messages, *produced])``
    写回 transcript 的"本轮传入"部分。若感知只在 prompt_vars（→system prompt），
    它不进 transcript、第二轮 replay 看不到。命门：感知必须在 USER message 里。
    """
    patched["unread"] = [
        _envelope("e1", "水壶在响"),
        _envelope("e2", "千凪在厨房煎蛋"),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    call = _FakeAgent.last_run()
    # 写回 transcript 的"本轮传入 messages"必须含她感知到的 event 原文
    msg_blob = "".join(m.text() for m in call["messages"])
    assert "水壶在响" in msg_blob, (
        "感知到的 event 必须进 USER message（=写回 session 的内容），"
        "否则第二轮 replay 看不到上一轮感知"
    )
    assert "千凪在厨房煎蛋" in msg_blob


@pytest.mark.asyncio
async def test_perceived_events_replayable_in_second_round(
    patched, fake_redis, monkeypatch
):
    """端到端续接：第一轮真写回 session，第二轮 load_session 能 replay 到上一轮感知。

    用真 ``append_session`` 把第一轮写回 fakeredis，再 ``load_session`` 读回，断言
    历史里含第一轮她感知到的 event 原文（不只固定文案）—— 这是"她真的记得自己经历
    过什么"的命门。
    """
    from app.agent.session import append_session, load_session
    from app.capabilities.redis import RedisCapability

    cap = RedisCapability(fake_redis)
    patched["unread"] = [_envelope("e1", "晨光斜照进房间")]

    captured: dict = {}

    async def _capture_then_persist(tools):
        # 拿到本轮真正传给 run 的 messages，模拟 core 的写回（本轮无工具调用 →
        # produced 只有最终助手回复，这里聚焦"本轮传入 messages"进了 transcript）。
        call = _FakeAgent.instances[-1].run_calls[-1]
        captured["messages"] = call["messages"]
        captured["session_id"] = call["session_id"]

    _FakeAgent.install(monkeypatch, script=_capture_then_persist)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    # 模拟 core 在成功后 append_session(session_id, [*messages, *produced])
    from app.agent.neutral import Message, Role

    await append_session(
        captured["session_id"],
        [*captured["messages"], Message(role=Role.ASSISTANT, content="ok")],
        cap=cap,
    )

    # 第二轮 load 这条 session：必须能 replay 到第一轮她感知到的 event 原文
    history = await load_session(captured["session_id"], cap=cap)
    replayed = "".join(m.text() for m in history)
    assert "晨光斜照进房间" in replayed, (
        "第二轮 replay 必须看到上一轮感知到的 event 原文，而非只有固定文案"
    )


# ---------------------------------------------------------------------------
# Task 2 —— life 上下文三层归位（spec 决策 4/5/6）。
#
# system prompt 收敛成纯静态身份（prompt_vars 只剩 persona_name / persona_lite）；
# 当轮新感知（几点 CST + 信箱动静）进 USER；上一刻状态正常靠意识流（transcript）
# 延续、只在意识流断了（transcript 空）时从 LifeState 兜底恢复。冷启探测复用
# world 的"节点自己 load_session 一次"模式（一致性靠 single_flight 锁）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_vars_only_static_identity(patched, monkeypatch):
    """prompt_vars 收敛成纯静态身份：只剩 persona_name / persona_lite。

    决策 4：current_time / prev_state / prev_mood / prev_activity 这些每轮都变的
    动态值全出 prompt_vars（→ system prompt），不再钉死在身份层。
    """
    patched["snapshot"] = LifeState(
        lane="coe-t3",
        persona_id="akao",
        current_state="睡觉",
        response_mood="困",
        activity_type="sleep",
        observed_at="2026-06-03T08:00:00Z",
    )
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    pv = _FakeAgent.last_run()["prompt_vars"]
    assert set(pv.keys()) == {"persona_name", "persona_lite"}, (
        f"prompt_vars 应只剩纯静态身份，实际键 {set(pv.keys())}"
    )
    for dynamic in ("current_time", "prev_state", "prev_mood", "prev_activity"):
        assert dynamic not in pv, f"{dynamic} 不该再在 prompt_vars 里（决策 4）"


@pytest.mark.asyncio
async def test_current_perception_in_user_message(patched, monkeypatch):
    """当轮新感知（几点 + 信箱动静）进 USER message，不走 prompt_vars→system。"""
    patched["unread"] = [
        _envelope("e1", "水壶在响"),
        _envelope("e2", "千凪在厨房"),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    call = _FakeAgent.last_run()
    msg_blob = "".join(m.text() for m in call["messages"])
    # 几点（带 CST 标识）+ 信箱动静都在 USER
    assert "CST" in msg_blob
    assert "水壶在响" in msg_blob
    assert "千凪在厨房" in msg_blob


@pytest.mark.asyncio
async def test_transcript_non_empty_no_recovery_segment(patched, monkeypatch):
    """transcript 非空（意识流没断）→ USER 不注入状态恢复段（状态在意识流里本就有）。

    决策 5：上一刻状态正常靠当天连续意识流延续；只有意识流断了才从 LifeState 兜底。
    """
    from app.agent.neutral import Message, Role

    patched["snapshot"] = LifeState(
        lane="coe-t3",
        persona_id="akao",
        current_state="在客厅看书很专注",
        response_mood="平静",
        activity_type="rest",
        observed_at="2026-06-03T08:00:00Z",
    )
    # 意识流非空：上一轮她说过做过的东西还在
    patched["transcript"] = [
        Message(role=Role.USER, content="上一轮的感知"),
        Message(role=Role.ASSISTANT, content="上一轮她的回应"),
    ]
    patched["unread"] = [_envelope("e1", "门铃响了")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # transcript 非空 → 不该把 LifeState 的 current_state 当"上次记得"重新喂
    assert "在客厅看书很专注" not in msg_blob, (
        "意识流非空时不该注入状态恢复段（状态在 transcript 里本就有，重复=冗余）"
    )
    # 但当轮新感知照常在 USER
    assert "门铃响了" in msg_blob


@pytest.mark.asyncio
async def test_transcript_empty_injects_recovery_segment(patched, monkeypatch):
    """transcript 空（冷启 / Redis 丢 / 跨天新 session）→ USER 含状态恢复段、带 snapshot 的 prev_state。

    决策 5：只判 transcript 空不空、不判 observed_at 是哪天（跨天先记得、不翻篇）。
    """
    patched["snapshot"] = LifeState(
        lane="coe-t3",
        persona_id="akao",
        current_state="在厨房煮咖啡",
        response_mood="慵懒",
        activity_type="rest",
        observed_at="2026-06-02T23:50:00Z",  # 哪怕是"昨晚"也照样恢复（只判空、不判日期）
    )
    patched["transcript"] = []  # 意识流断了
    patched["unread"] = [_envelope("e1", "新的一天的光")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 恢复段必须带 snapshot 的 current_state（她"上次记得在做"什么）
    assert "在厨房煮咖啡" in msg_blob, (
        "意识流断了时必须从 LifeState 恢复上次状态喂进 USER（不彻底失忆）"
    )
    # 当轮新感知照常在
    assert "新的一天的光" in msg_blob


@pytest.mark.asyncio
async def test_transcript_empty_no_snapshot_no_crash(patched, monkeypatch):
    """transcript 空且从没有过 LifeState（snapshot=None）→ 不崩、不硬塞假状态。"""
    patched["snapshot"] = None
    patched["transcript"] = []
    patched["unread"] = [_envelope("e1", "第一次睁眼")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    call = _FakeAgent.last_run()
    msg_blob = "".join(m.text() for m in call["messages"])
    # 当轮新感知照常在；没有 snapshot 就别恢复（兜底不崩即可）
    assert "第一次睁眼" in msg_blob


@pytest.mark.asyncio
async def test_cold_start_probe_uses_round_session_id(patched, monkeypatch):
    """冷启探测用的 session_id 与 run 续接的 session_id 是同一个（同 (lane, persona, 今天)）。

    决策 5/双读一致性：节点自己 load_session 探 transcript 空不空，探测用的 session_id
    必须就是 run 续接那条——否则探的是空的、run 续的是另一条，双读不一致。
    """
    seen: dict = {}

    async def _probe_load(session_id, **kwargs):
        seen["probe_session_id"] = session_id
        return list(patched["transcript"])

    monkeypatch.setattr(lw, "load_session", _probe_load)

    patched["transcript"] = []
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    run_session_id = _FakeAgent.last_run()["session_id"]
    assert seen["probe_session_id"] == run_session_id, (
        "冷启探测的 session_id 必须与 run 续接的 session_id 一致（双读同一条 transcript）"
    )
