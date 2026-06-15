"""life_wake_node — 三姐妹同构 life 节点 (Task 3, agent 工具循环).

被 EventArrived 攒批唤醒后她跑一个 ReAct 循环：读自己 LifeState（主观快照）+ 读
自己信箱未读 event → 喂进 ``Agent(...).run`` 跑工具循环（连续调 update_life_state /
act 行动）→ 收口标已读（只标本轮读到的 event_id）。输出来自工具调用，不再填一张
LifeDecision 表。

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
    EVENT_KIND_MESSAGE,
    EVENT_KIND_SPEECH,
    EVENT_KIND_SURROUNDINGS,
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
    event_id,
    summary,
    *,
    kind=EVENT_KIND_AMBIENT,
    occurred_at="2026-06-03T12:30:00Z",
    source="world",
    persona_id="akao",
):
    return EventEnvelope(
        lane="coe-t3",
        persona_id=persona_id,
        event_id=event_id,
        kind=kind,
        source=source,
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
    perform_act handler 在 life_tools 模块里被打桩成记录副作用。

    世界阶段透传走 ``app.domain.arc_awareness``（render 真跑，底下的
    ``read_world_arc`` 打桩）：默认 ``arc=None``（空链 → 整段缺席），测试可设
    ``state["arc"]`` 注入一版世界阶段；``arc_lanes`` 记录按哪个 lane 读的。
    """
    import app.domain.arc_awareness as arc_mod
    import app.nodes.life_tools as lt

    state = {
        "snapshot": None,  # find_life_state 返回
        "unread": [],  # list_unread_events 返回
        "saved": [],  # save_life_state 收到的
        "marked": [],  # mark_events_read 收到的 event_ids
        "acts": [],  # perform_act 收到的
        "transcript": [],  # load_session 探测返回（空=冷启）
        "arc": None,  # read_world_arc 返回（None=空链）
        "arc_lanes": [],  # read_world_arc 收到的 lane
        "notebook": [],  # list_notebook_entries 返回（[]=空本子）
        "notebook_calls": [],  # list_notebook_entries 收到的 kwargs
        "notebook_raises": None,  # 非 None → list_notebook_entries 抛它（读失败）
    }

    async def fake_read_world_arc(*, lane):
        state["arc_lanes"].append(lane)
        return state["arc"]

    monkeypatch.setattr(arc_mod, "read_world_arc", fake_read_world_arc)

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

    async def fake_act(**kwargs):
        state["acts"].append(kwargs)

    async def fake_load_persona(persona_id):
        from app.memory._persona import PersonaContext

        return PersonaContext(
            persona_id=persona_id,
            display_name=persona_id,
            persona_lite=f"{persona_id} 的人设",
        )

    # 「她最近一页昨天」注入读口：每轮都会读（不再依赖 marker），打桩成无页
    # ——本文件不测注入形态（那在 test_life_wake_day_review.py），只防真连 PG。
    async def fake_read_day_page_before(*, lane, persona_id, before_date):
        return None

    monkeypatch.setattr(lw, "find_life_state", fake_find)
    monkeypatch.setattr(lw, "list_unread_events", fake_unread)
    monkeypatch.setattr(lw, "mark_events_read", fake_mark)
    monkeypatch.setattr(lw, "load_persona", fake_load_persona)
    # 「她本子里还没了结的事」注入读口：每轮按 active_only=True 读还活着的条目。
    # 打桩成可注入 entries / 记录调用 / 可抛错（读失败 → 整段缺席不炸轮）。
    async def fake_list_notebook_entries(*, lane, persona_id, active_only):
        state["notebook_calls"].append(
            {"lane": lane, "persona_id": persona_id, "active_only": active_only}
        )
        if state["notebook_raises"] is not None:
            raise state["notebook_raises"]
        return list(state["notebook"])

    monkeypatch.setattr(lw, "load_session", fake_load_session)
    monkeypatch.setattr(lw, "read_day_page_before", fake_read_day_page_before)
    monkeypatch.setattr(lw, "list_notebook_entries", fake_list_notebook_entries)
    # 工具底下的 durable handler：在 life_tools 模块里打桩。
    monkeypatch.setattr(lt, "save_life_state", fake_save)
    monkeypatch.setattr(lt, "perform_act", fake_act)

    # note / edit_note 底下的 durable handler 也打桩（第三块：note 工具落库成功后才把
    # 待挂日程提醒记进 round-scoped 容器，所以引擎侧测 fire_schedule_reminders 时这两个
    # handler 必须成功、不真连 PG）。
    async def fake_note_entry(**kwargs):
        state["noted"].append(kwargs)

    async def fake_update_entry(**kwargs):
        state["edited"].append(kwargs)

    state.setdefault("noted", [])
    state.setdefault("edited", [])
    monkeypatch.setattr(lt, "note_entry", fake_note_entry)
    monkeypatch.setattr(lt, "update_entry", fake_update_entry)
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


def _script_update_then_act(description="我去厨房煮咖啡"):
    async def _run(tools):
        by_name = {t.name: t for t in tools}
        await by_name["update_life_state"].invoke(
            {"current_state": "醒了", "response_mood": "迷糊", "activity_type": "move"}
        )
        await by_name["act"].invoke({"description": description})

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
async def test_act_occurred_at_is_cst_aware_iso(patched, monkeypatch):
    """act 的 occurred_at 是 observed_at 的 pass-through → 也跟着 CST aware。"""
    from app.infra import cst_time

    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(
        monkeypatch, script=_script_update_then_act(description="我去厨房")
    )

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(patched["acts"]) == 1
    occ = patched["acts"][0]["occurred_at"]
    assert "+08:00" in occ, f"act occurred_at 该 CST aware，实际 {occ!r}"
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


# ---------------------------------------------------------------------------
# 1C Task 2：world 五官 —— life stimulus 呈现「此刻你周遭」（周遭切片 vs 动静分层）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surroundings_event_rendered_as_this_moment_around_you(patched, monkeypatch):
    """信箱里 kind=surroundings 的周遭切片 → stimulus 里呈现成「此刻你周遭」段。

    1C Task 2：world 给每个 life 投更丰富的周遭客观切片（你在哪、谁在你身边、环境
    怎样），life 醒来读到的不再只是零碎「动静」，而是「此刻你周遭：……」的客观叙事，
    据此自主行动（不用问就知道周遭有谁）。
    """
    patched["unread"] = [
        _envelope(
            "s1",
            "你在客厅写作业，厨房飘来赤尾做饭的香味，午后的光斜照进来。",
            kind=EVENT_KIND_SURROUNDINGS,
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 周遭切片原文进 stimulus（→进 transcript→第二轮可 replay）
    assert "你在客厅写作业，厨房飘来赤尾做饭的香味，午后的光斜照进来。" in msg_blob
    # 呈现成「此刻你周遭」的框（而不是混在零碎动静清单里）
    assert "此刻你周遭" in msg_blob, (
        "周遭切片应呈现成「此刻你周遭」段，让她感知到所处环境，而非零碎动静"
    )
    # 这条照常标已读
    assert patched["marked"] == [["s1"]]


@pytest.mark.asyncio
async def test_surroundings_and_ambient_rendered_in_separate_sections(patched, monkeypatch):
    """周遭切片与离散动静分层呈现：周遭进「此刻你周遭」、动静进「动静」段，互不混淆。

    周遭（surroundings）是 world 推演的「此刻你所处的环境」客观叙事；离散动静
    （ambient）是「环境里出现的某个新声响光线气味」。两者语义不同——周遭是底框、
    动静是其上发生的事——life 分层呈现，让她既知道自己周遭什么样、又知道刚发生了
    什么动静。
    """
    patched["unread"] = [
        _envelope(
            "s1",
            "你在客厅，姐姐们在厨房忙活。",
            kind=EVENT_KIND_SURROUNDINGS,
        ),
        _envelope("a1", "玄关传来开关门的声音", kind=EVENT_KIND_AMBIENT),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    assert "你在客厅，姐姐们在厨房忙活。" in msg_blob
    assert "玄关传来开关门的声音" in msg_blob
    # 分两段呈现：周遭段（「此刻你周遭」）+ 动静段（「这会儿你还感知到」），各有标题
    assert "此刻你周遭" in msg_blob
    assert "这会儿你还感知到" in msg_blob, "离散动静应单列动静段、不混进周遭段"
    # 结构正确：周遭段标题在前、动静段标题在后；周遭文字落在周遭段（两标题之间），
    # 离散动静文字落在动静段（动静段标题之后）。
    around_idx = msg_blob.index("此刻你周遭")
    dynamics_idx = msg_blob.index("这会儿你还感知到")
    assert around_idx < dynamics_idx
    surroundings_section = msg_blob[around_idx:dynamics_idx]
    dynamics_section = msg_blob[dynamics_idx:]
    assert "你在客厅，姐姐们在厨房忙活。" in surroundings_section
    assert "玄关传来开关门的声音" not in surroundings_section, (
        "离散动静不该被塞进「此刻你周遭」段"
    )
    assert "玄关传来开关门的声音" in dynamics_section
    assert "你在客厅，姐姐们在厨房忙活。" not in dynamics_section, (
        "周遭切片不该被塞进动静段"
    )
    # 两条都标已读
    assert patched["marked"] == [["s1", "a1"]]


# ---------------------------------------------------------------------------
# 刀 3 Task 4：周遭感知的「认知留白」维度 —— 角色对他人此刻在哪不把过时信息当确定。
#
# 穿帮：角色对「别人此刻在哪」的认知全来自 world 的 surroundings 周遭切片，但她对
# 这条信息没有「我是什么时候感知到的、可能已经过时」的留白，于是把可能过时的位置
# 当既成事实（千凪对明写「仍在家」的赤尾说「刚出门慢点走」，把别人此刻的位置当确定）。
#
# 修法是优化输入、不是加位置校验 if：surroundings 段带上「这是你**上一次**感知到的
# 周遭、感知于 X 分钟前」的时间锚，让她自然意识到她对别人位置的认知是某个过去时刻
# 的快照、可能已变（像系统本能做对的「你要是还在家就慢点走」那种留白），而不是断言。
#
# 绝不比对 world 真相纠错（那是确定性规则、违反赤尾宪法）：这里只给她她自己感知的
# 时间锚（她够得着的、纯主观的「我多久前感知的」），由她凭这个自己留白。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_surroundings_section_carries_perceived_time_anchor(patched, monkeypatch):
    """周遭段必须带「这是你上一次感知到的周遭、感知于多久前」的时间锚（认知留白维度）。

    红：现状 surroundings 段只客观铺开「此刻你周遭：……」，没有任何「我是什么时候
    感知到的」的留白，于是模型把切片里别人的位置当此刻既成事实。绿：周遭段框成「你
    **上一次**感知到的周遭」并带上感知时刻相对 now 的时间锚（多久前），让她意识到这
    是个过去快照、别人此刻位置可能已变。

    钉死 now 让「多久前」可断言：感知切片 occurred_at 比 now 早 25 分钟。
    """
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            # 真实 UTC 12:30 这个时刻，按传入 tz 表示（→ CST 20:30）。
            base = cls(2026, 6, 3, 12, 30, tzinfo=_dt.timezone.utc)
            return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)

    monkeypatch.setattr(lw.cst_time, "datetime", _FixedDateTime)

    # 周遭切片感知于 25 分钟前（UTC 12:05 → CST 20:05），now 是 CST 20:30。
    patched["unread"] = [
        _envelope(
            "s1",
            "你在客厅写作业，绫奈姐姐在沙发上看书。",
            kind=EVENT_KIND_SURROUNDINGS,
            occurred_at="2026-06-03T12:05:00Z",
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 周遭切片原文照常进 stimulus
    assert "你在客厅写作业，绫奈姐姐在沙发上看书。" in msg_blob
    # 框成「上一次感知到的周遭」——明确这是过去快照、不是此刻保证（认知留白命门）
    assert "上一次" in msg_blob, (
        "周遭段必须框成「你上一次感知到的周遭」，让她意识到这是过去快照、别人位置可能已变"
    )
    # 带上感知时刻相对 now 的时间锚：感知于 25 分钟前
    assert "25 分钟前" in msg_blob, (
        "周遭段必须带「感知于 X 分钟前」的时间锚（认知留白维度），实际 "
        f"{msg_blob!r}"
    )


@pytest.mark.asyncio
async def test_surroundings_anchor_uses_latest_slice_when_multiple(patched, monkeypatch):
    """一轮攒了多版周遭切片时，时间锚按**最新那版**的感知时刻算（她对周遭的最新认知）。

    周遭切片是底框、末尾那版最新。时间锚应反映「她最近一次感知到周遭」是多久前——
    用最新切片的 occurred_at，而不是最早那版（否则留白会过度、把刚感知的也说成很旧）。
    """
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 6, 3, 12, 30, tzinfo=_dt.timezone.utc)
            return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)

    monkeypatch.setattr(lw.cst_time, "datetime", _FixedDateTime)

    patched["unread"] = [
        _envelope(
            "s1", "更早的一版周遭：大家都在客厅。",
            kind=EVENT_KIND_SURROUNDINGS, occurred_at="2026-06-03T11:30:00Z",  # 60 分钟前
        ),
        _envelope(
            "s2", "较新的一版周遭：绫奈去厨房了。",
            kind=EVENT_KIND_SURROUNDINGS, occurred_at="2026-06-03T12:25:00Z",  # 5 分钟前
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 时间锚按最新那版（5 分钟前）算，不是最早那版（60 分钟前）
    assert "5 分钟前" in msg_blob, "时间锚应按最新切片的感知时刻算"
    assert "60 分钟前" not in msg_blob, "不应按最早那版算时间锚（会过度留白）"


@pytest.mark.asyncio
async def test_surroundings_anchor_just_now_when_fresh(patched, monkeypatch):
    """刚感知到的周遭（时间锚约等于此刻）不强行制造留白：说成「刚刚」而非「X 分钟前」。

    认知留白是「信息可能过时」时才需要的；周遭切片就是此刻刚感知的（occurred_at ≈ now）
    时不该硬塞「N 分钟前」让她对刚感知的也犯嘀咕。just-now 走「刚刚」措辞，保留留白维度
    的诚实（多久前就是多久前，刚感知就是刚感知）。
    """
    import datetime as _dt

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            base = cls(2026, 6, 3, 12, 30, tzinfo=_dt.timezone.utc)
            return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)

    monkeypatch.setattr(lw.cst_time, "datetime", _FixedDateTime)

    patched["unread"] = [
        _envelope(
            "s1", "你在客厅，绫奈在你旁边。",
            kind=EVENT_KIND_SURROUNDINGS, occurred_at="2026-06-03T12:30:00Z",  # = now
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    assert "刚刚" in msg_blob, "刚感知到的周遭时间锚应说「刚刚」，不硬塞 N 分钟前"


@pytest.mark.asyncio
async def test_surroundings_anchor_unparseable_occurred_at_no_crash(patched, monkeypatch):
    """周遭切片 occurred_at 脏 / 无法解析时不崩、不硬编时间锚（兜底降级）。

    occurred_at 脏（解析失败）→ 算不出「多久前」。此时不崩、照常铺出周遭原文，只是
    这一版不带可信时间锚（认知留白靠 prompt 里「这是你上一次感知到的周遭」的框来兜，
    不靠这个算不出的数字）。
    """
    patched["unread"] = [
        _envelope(
            "s1", "你在客厅，绫奈在沙发上。",
            kind=EVENT_KIND_SURROUNDINGS, occurred_at="不是时间的脏串",
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    # 不该抛
    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 周遭原文照常铺出（兜底不吞）
    assert "你在客厅，绫奈在沙发上。" in msg_blob
    # 仍框成「上一次感知到的周遭」（留白框不依赖能否算出 N 分钟前）
    assert "上一次" in msg_blob


def test_format_surroundings_no_world_truth_comparison():
    """结构命门：认知留白只用她自己感知切片的时间锚，绝不比对 world 真相纠错。

    赤尾宪法：这是优化输入、不是加位置校验 if。``_format_surroundings`` 的签名只接
    她自己的 surroundings 切片 + now，绝不接 WorldState / presence / 别人的 LifeState
    —— 结构上杜绝「比对 world 发现不一致就拦截 / 纠正」那类确定性规则。
    """
    import inspect

    sig = inspect.signature(lw._format_surroundings)
    params = list(sig.parameters)
    # 只接 surroundings 切片 + now（她自己够得着的），不接任何 world 真相参数
    for forbidden in ("world", "world_state", "presence", "others", "life_states"):
        assert forbidden not in params, (
            f"_format_surroundings 不该接 world 真相参数 {forbidden}（那是位置校验、违反赤尾宪法）"
        )
    # 源码里不出现任何「比对真相纠错」的符号
    src = inspect.getsource(lw._format_surroundings)
    for forbidden in ("WorldState", "read_presence", "find_life_state"):
        assert forbidden not in src, (
            f"_format_surroundings 引用了 world 真相符号 {forbidden}（位置校验、违反赤尾宪法）"
        )


# ---------------------------------------------------------------------------
# 1C Task 3：角色直连对话 —— 收件人 stimulus 呈现「X 对你说：原话」（speech 分层）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_speech_event_rendered_as_someone_said_to_you(patched, monkeypatch):
    """① 直投链路（收件人侧）：信箱里 kind=speech 的对话 → stimulus 呈现「X 对你说：原话」。

    1C Task 3：另一姐妹 chat 给她的原话直投进她信箱（kind=speech、source=说话者），
    她醒来在 stimulus 里读到「赤尾对你说：原话」——原话原样呈现（非 world 换词版）。
    """
    # speech event 的 source 是说话者 persona_id（akao）—— 渲染「X 对你说」要用它。
    patched["unread"] = [
        _envelope(
            "sp1",
            "绫奈姐姐你在做什么好吃的呀",
            kind=EVENT_KIND_SPEECH,
            source="akao",
            persona_id="ayana",
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="ayana"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 原话原样进 stimulus（非 world 换词版）
    assert "绫奈姐姐你在做什么好吃的呀" in msg_blob
    # 呈现成「X 对你说」（带说话者身份），不混进周遭底框 / 零碎动静
    assert "对你说" in msg_blob, "speech 应呈现成「X 对你说：原话」"
    # 带说话者身份（akao）
    assert "akao" in msg_blob, "speech 要带说话者身份"


@pytest.mark.asyncio
async def test_speech_rendered_separately_from_surroundings_and_dynamics(
    patched, monkeypatch
):
    """speech 是独立一层，不混进「此刻你周遭」底框、也不混进离散动静段。

    三类语义不同：周遭（surroundings）是底框、动静（ambient）是其上的离散事件、
    speech 是"有人直接对你说的话"。分层呈现，speech 单列自己的段。
    """
    patched["unread"] = [
        _envelope(
            "s1", "你在客厅，午后的光斜照进来。", kind=EVENT_KIND_SURROUNDINGS,
            persona_id="ayana",
        ),
        _envelope(
            "a1", "玄关传来开关门的声音", kind=EVENT_KIND_AMBIENT, persona_id="ayana",
        ),
        _envelope(
            "sp1", "绫奈姐姐你在做什么好吃的呀", kind=EVENT_KIND_SPEECH,
            source="akao", persona_id="ayana",
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="ayana"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    around_idx = msg_blob.index("此刻你周遭")
    surroundings_section = msg_blob[
        around_idx : msg_blob.index("绫奈姐姐你在做什么好吃的呀")
    ]
    # speech 原话不该被塞进「此刻你周遭」底框
    assert "绫奈姐姐你在做什么好吃的呀" not in surroundings_section, (
        "speech 原话不该混进「此刻你周遭」底框"
    )
    # speech 段带「对你说」标识，与离散动静的「[ambient]」清单分开
    assert "对你说" in msg_blob
    # 三条都标已读
    assert patched["marked"] == [["s1", "a1", "sp1"]]


@pytest.mark.asyncio
async def test_npc_speech_rendered_with_clean_name_not_machine_prefix(
    patched, monkeypatch
):
    """NPC 来访（source=npc:名字、kind=speech）→ stimulus 呈现「林小满 对你说：原话」。

    NPC 层第二刀：world 以具名 NPC 身份投的 event，source 在信箱里是机器约定
    ``npc:林小满``（对齐第一刀 npc_name + 关系页 npc:xxx），但喂给模型时要呈现成干净
    的人名「林小满 对你说：…」——``npc:`` 是机读前缀（关系页 keying 用），不该漏给模型
    看。她据此识别「是 NPC 林小满来找我」：不被当真人（真人是 user:xxx / kind=external、
    走离散动静段）、也不被当 world 环境动静（ambient）。
    """
    patched["unread"] = [
        _envelope(
            "npc1",
            "绫奈周末有空吗？一起去图书馆吧。",
            kind=EVENT_KIND_SPEECH,
            source="npc:林小满",
            persona_id="ayana",
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="ayana"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 原话原样进 stimulus
    assert "绫奈周末有空吗？一起去图书馆吧。" in msg_blob
    # 呈现成「林小满 对你说」（speech 段，不混进离散动静 / 周遭底框）
    assert "林小满 对你说" in msg_blob
    # 机读前缀 npc: 不漏给模型
    assert "npc:林小满" not in msg_blob, "npc: 机读前缀不该出现在喂给模型的 stimulus 里"


# ---------------------------------------------------------------------------
# task 5（通信介质维度，life 侧）：kind=message 的手机消息 → stimulus 呈现
# 「X 给你发消息：内容」，与当面 speech 的「X 对你说：原话」**收件人侧可区分**
# （spec 决策 5 / 7：否则又把「当面还是手机」混为一谈，task 5 白做）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_message_event_rendered_as_someone_messaged_you(patched, monkeypatch):
    """信箱里 kind=message 的手机消息 → stimulus 呈现「X 给你发消息：内容」。

    另一姐妹不在一起时用 send_message 隔空发来的消息（kind=message、source=发送者
    persona_id，task 3），她醒来在 stimulus 里读到「赤尾给你发消息：内容」——明确是
    隔着手机/飞书发来的，不是当面说的（区别于 speech 的「X 对你说」）。
    """
    patched["unread"] = [
        _envelope(
            "msg1",
            "绫奈我到广州啦，晚点视频",
            kind=EVENT_KIND_MESSAGE,
            source="akao",
            persona_id="ayana",
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="ayana"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 内容原样进 stimulus
    assert "绫奈我到广州啦，晚点视频" in msg_blob
    # 呈现成「X 给你发消息」（通信介质 = 隔空发的消息），带发送者身份
    assert "给你发消息" in msg_blob, "message 应呈现成「X 给你发消息：内容」"
    assert "akao" in msg_blob, "message 要带发送者身份"
    # 通信介质维度要标清「隔着手机 / 不在一起」（区别于当面），让她不把消息当当面
    assert "手机" in msg_blob, "message 段要标明这是隔着手机发来的消息（通信介质维度）"


@pytest.mark.asyncio
async def test_message_distinct_from_speech_face_to_face(patched, monkeypatch):
    """同一轮里手机消息（message）与当面话（speech）必须呈现成**两种**形态。

    spec 决策 5 / 7 命门：手机消息「X 给你发消息」与当面话「X 对你说」是收件人侧
    两个正交模态，绝不混成一句。否则又把「当面还是手机」混为一谈。
    """
    patched["unread"] = [
        _envelope(
            "sp1", "绫奈你看这个", kind=EVENT_KIND_SPEECH,
            source="chinagi", persona_id="ayana",
        ),
        _envelope(
            "msg1", "绫奈我到广州啦", kind=EVENT_KIND_MESSAGE,
            source="akao", persona_id="ayana",
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="ayana"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # 当面话用「对你说」、手机消息用「给你发消息」，两种形态都在、可区分
    assert "对你说" in msg_blob, "speech 仍呈现成「X 对你说」（当面）"
    assert "给你发消息" in msg_blob, "message 呈现成「X 给你发消息」（隔空）"
    # 手机消息的内容不该被「对你说」框住（不混进当面话那一句）
    speech_idx = msg_blob.index("对你说")
    speech_line = msg_blob[speech_idx:msg_blob.index("绫奈你看这个") + len("绫奈你看这个")]
    assert "绫奈我到广州啦" not in speech_line, "手机消息内容不该混进当面话「对你说」一句里"
    # 两条都标已读
    assert patched["marked"] == [["sp1", "msg1"]]


@pytest.mark.asyncio
async def test_message_not_lumped_into_ambient_dynamics(patched, monkeypatch):
    """kind=message 不该被误归进离散动静（ambient）桶（task 5 必改 ①）。

    旧 _split_perception 把「非 surroundings/speech」一律归 dynamics，新 message
    kind 会被误塞进「[message] ...」离散动静清单。message 是直接冲她来的消息、有
    明确发送人，必须独立成段，不混进环境动静。
    """
    patched["unread"] = [
        _envelope(
            "a1", "玄关传来开关门的声音", kind=EVENT_KIND_AMBIENT, persona_id="ayana",
        ),
        _envelope(
            "msg1", "绫奈我到广州啦", kind=EVENT_KIND_MESSAGE,
            source="akao", persona_id="ayana",
        ),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="ayana"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    # message 内容绝不带 [message] 机读 kind 前缀（那是离散动静清单的形态）
    assert "[message]" not in msg_blob, "message 不该呈现成离散动静的「[message] ...」清单项"
    # message 内容也不该出现在「[ambient]」那种离散动静行里
    assert "[ambient] " in msg_blob or "玄关传来开关门的声音" in msg_blob
    # message 仍以「给你发消息」独立形态呈现
    assert "给你发消息" in msg_blob


def test_split_perception_four_way_message_separate_from_dynamics():
    """_split_perception 把未读四分：周遭 / 当面话 / 手机消息 / 离散动静（task 5 必改 ①）。

    message kind 必须单独成一桶、绝不和 ambient 混进 dynamics —— 否则 _format_dynamics
    会把手机消息当离散动静渲染（「[message] ...」），又制造一遍当面/手机混淆。
    """
    unread = [
        _envelope("s1", "你在客厅", kind=EVENT_KIND_SURROUNDINGS),
        _envelope("sp1", "当面说的话", kind=EVENT_KIND_SPEECH, source="akao"),
        _envelope("msg1", "手机发来的", kind=EVENT_KIND_MESSAGE, source="chinagi"),
        _envelope("a1", "开关门声", kind=EVENT_KIND_AMBIENT),
    ]
    surroundings, speech, messages, dynamics = lw._split_perception(unread)
    assert [e.event_id for e in surroundings] == ["s1"]
    assert [e.event_id for e in speech] == ["sp1"]
    assert [e.event_id for e in messages] == ["msg1"], "message 必须单独成一桶"
    assert [e.event_id for e in dynamics] == ["a1"], "dynamics 只剩 ambient，不含 message"


@pytest.mark.asyncio
async def test_acts_when_model_calls_it(patched, monkeypatch):
    """模型在循环里调 act → 落 ActPerformed，per-act id 从本轮 base act_id 派生。"""
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(
        monkeypatch, script=_script_update_then_act(description="我起床去厨房做早饭")
    )

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(patched["acts"]) == 1
    assert patched["acts"][0]["persona_id"] == "akao"
    assert patched["acts"][0]["description"] == "我起床去厨房做早饭"
    assert patched["acts"][0]["lane"] == "coe-t3"
    # base act_id 仍按本轮 event_ids 派生（整轮重投幂等不退化）；工具给第 1 件 act
    # 派 per-act id = uuid5(base, "...:1")。
    import uuid

    base = str(uuid.uuid5(uuid.NAMESPACE_DNS, "coe-t3:akao:e1"))
    expected = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{base}:1"))
    assert patched["acts"][0]["act_id"] == expected


def _script_act_twice(description1="我去厨房煮咖啡", description2="再去叫醒千凪"):
    async def _run(tools):
        by_name = {t.name: t for t in tools}
        await by_name["act"].invoke({"description": description1})
        await by_name["act"].invoke({"description": description2})

    return _run


@pytest.mark.asyncio
async def test_two_acts_in_one_round_both_emit_distinct_ids(patched, monkeypatch):
    """一轮里模型调两次 act：两件都真正做事、各落一条 ActPerformed、各自唯一 id。

    P6 修复：删掉"一轮只生效一件"的 if 守卫（那是用 if 替角色决策、违反赤尾宪法）。
    per-act id 用 base act_id + 本轮第 N 件序号派生：两件序号不同 → 两个不同 id →
    insert_idempotent 不再把第二件按同 id 去重吞掉。一轮想做几件就做几件。
    """
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(
        monkeypatch,
        script=_script_act_twice(description1="我起床去厨房", description2="再去叫醒千凪"),
    )

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    # 两件都落到 handler（不再"一轮只生效一件"）
    assert len(patched["acts"]) == 2
    assert [a["description"] for a in patched["acts"]] == ["我起床去厨房", "再去叫醒千凪"]
    assert all(a["persona_id"] == "akao" for a in patched["acts"])
    assert all(a["lane"] == "coe-t3" for a in patched["acts"])
    # 两件各自唯一 act_id（per-act 派生）
    act_ids = [a["act_id"] for a in patched["acts"]]
    assert len(set(act_ids)) == 2, f"两件 act 应各自唯一 id，实得 {act_ids}"
    # 两件 id 都从本轮 base（event_ids 派生）+ 序号派生
    import uuid

    base = str(uuid.uuid5(uuid.NAMESPACE_DNS, "coe-t3:akao:e1"))
    assert act_ids == [
        str(uuid.uuid5(uuid.NAMESPACE_OID, f"{base}:1")),
        str(uuid.uuid5(uuid.NAMESPACE_OID, f"{base}:2")),
    ]
    # 收口照常标已读
    assert patched["marked"] == [["e1"]]


@pytest.mark.asyncio
async def test_no_act_when_model_doesnt_call_it(patched, monkeypatch):
    """没调 act 就不回灌 world（她只是默默换了个状态）。"""
    patched["unread"] = [_envelope("e1", "外面在下雨")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert patched["acts"] == []


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
    assert patched["acts"] == []

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
    # cd 内绝不跑：不建 Agent、不写、不标已读、不做事
    assert _FakeAgent.instances == [], "cd 内不该烧模型（不建 Agent）"
    assert patched["saved"] == []
    assert patched["marked"] == []
    assert patched["acts"] == []


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


@pytest.mark.asyncio
async def test_event_wake_in_cd_still_raises_debounce_reschedule(
    patched, fake_redis, monkeypatch
):
    """event wake 命中 cd → raise DebounceReschedule，让 debounce handler 把这批 event 攒到 cd 后。

    cd 内不烧模型、不写、不标已读，但绝不 drop 这批 event：走 debounce wire 的 raise
    DebounceReschedule 把它推到 cd 后再醒一次（攒着、不丢）。
    """
    arrived = EventArrived(lane="coe-t3", persona_id="akao")
    await fake_redis.set(lw._cd_key("coe-t3", "akao"), "1", ex=lw._LIFE_CD_SECONDS)
    patched["unread"] = [_envelope("e1", "cd 内来的动静")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    with pytest.raises(DebounceReschedule) as ei:
        await lw.life_wake_node(arrived)

    assert ei.value.data is arrived
    # cd 内不烧模型、不写、不标已读（攒着、不丢）
    assert _FakeAgent.instances == []
    assert patched["saved"] == []
    assert patched["marked"] == []


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


@pytest.mark.integration
async def test_perceived_events_replayable_in_second_round(
    patched, fake_redis, test_db, monkeypatch
):
    """端到端续接：第一轮真写回 session，第二轮 load_session 能 replay 到上一轮感知。

    用真 ``append_session`` 把第一轮写回 PG durable transcript，再 ``load_session``
    读回，断言历史里含第一轮她感知到的 event 原文（不只固定文案）—— 这是"她真的
    记得自己经历过什么"的命门。
    """
    from app.agent.session import append_session, load_session
    from app.domain.session_transcript import SessionTranscript
    from tests.runtime.conftest import migrate

    await migrate(SessionTranscript, test_db)
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
    )

    # 第二轮 load 这条 session：必须能 replay 到第一轮她感知到的 event 原文
    history = await load_session(captured["session_id"])
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


# ---------------------------------------------------------------------------
# world-driven wake —— 唤醒只剩 world notify 一条腿（EventArrived → life_wake_node）。
#
# 角色的自排执行腿（LifeWakeTick → life_self_wake_node + 到点 gate）和被否的 fan-out
# 定时心跳整套已拆掉，只保留 schedule 写下 next_wake_at 意愿这半（world 每轮读它推演谁
# 该叫）。这里钉死：EventArrived 永远放行（即使排了未来 next_wake_at）、event 唤醒一轮
# 收口仍写 next_wake_at（自排意愿落库）、被叫醒入口（EventArrived）行为不退化。
# ---------------------------------------------------------------------------


def _now_cst():
    from app.infra import cst_time

    return cst_time.now_cst()


def _stub_life_state(patched, snapshot):
    """覆盖 patched fixture 的 find_life_state 返回值。"""
    patched["snapshot"] = snapshot


# --- EventArrived 永远放行（外部刺激不走任何到点 gate） ---


@pytest.mark.asyncio
async def test_event_wake_always_passes_even_with_future_next_wake(patched, monkeypatch):
    """EventArrived（world notify 入口）永远放行 —— 哪怕 LifeState 排了一个未来才到的 next_wake_at。

    world-driven wake：角色被叫醒只剩 EventArrived 这一条入口（world notify 走它）。
    它不走任何到点 gate、永远立刻醒——这是新架构里角色唯一的醒来路径，必须不退化。
    """
    future = (_now_cst() + __import__("datetime").timedelta(minutes=30)).isoformat()
    _stub_life_state(
        patched,
        LifeState(
            lane="coe-t3", persona_id="akao",
            current_state="在写作业", response_mood="专注", activity_type="study",
            observed_at="2026-06-03T08:00:00Z", next_wake_at=future,
        ),
    )
    patched["unread"] = [_envelope("e1", "门铃响了")]
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert _FakeAgent.instances, "EventArrived 外部刺激必须永远放行（即使排了未来 next_wake_at）"


# --- 空信箱语义：event 空信箱仍 early return（没新动静不用跑） ---


@pytest.mark.asyncio
async def test_event_wake_empty_inbox_still_early_returns(patched, monkeypatch):
    """event 唤醒空信箱仍 early return（没新动静不用跑）—— 现有行为不退化。"""
    patched["unread"] = []
    _FakeAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert _FakeAgent.instances == [], "event 唤醒空信箱仍不烧模型（early return 不变）"
    assert patched["saved"] == []
    assert patched["marked"] == []


# --- act / 幂等种子：event 唤醒 base act_id 按本轮 event_ids 派生（重放幂等） ---


@pytest.mark.asyncio
async def test_event_wake_act_id_seed_unchanged(patched, monkeypatch):
    """event 唤醒的 base act_id 种子语义不退化：仍按本轮 event_ids 派生（重放幂等）。

    P6 修复后 per-act id 多套一层 uuid5(base, 序号)，但 **base 种子必须仍来自
    event_ids**（不能退回 now 派生，否则重投得新 base → 新 per-act id → world 重复
    推演）。这里钉死：第 1 件 act 的 id == uuid5(base, "...:1") 且 base ==
    uuid5(DNS, "coe-t3:akao:e1") —— 若 base 改从 now 派生，这个等式会断。
    """
    patched["unread"] = [_envelope("e1", "天亮了")]
    _FakeAgent.install(
        monkeypatch, script=_script_update_then_act(description="我起床去厨房做早饭")
    )

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(patched["acts"]) == 1
    import uuid

    base = str(uuid.uuid5(uuid.NAMESPACE_DNS, "coe-t3:akao:e1"))
    expected = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{base}:1"))
    assert patched["acts"][0]["act_id"] == expected, (
        "event 唤醒的 base act_id 种子必须仍按本轮 event_ids 派生（重放幂等不退化）"
    )


# --- 收口：event 唤醒跑完写下她想几点醒的意愿（next_wake_at），交给 world ---


@pytest.mark.asyncio
async def test_event_wake_round_also_fires_self_wake(patched, monkeypatch):
    """event 唤醒一轮跑完走收口 fire 写下 next_wake_at 意愿（world 每轮读它推演谁该叫）。

    world-driven wake：被 world notify 起头叫醒后，她用 schedule 写下「想几点醒」的意愿，
    收口 fire_life_self_wake 把它落进 next_wake_at（只写意愿、不再 emit 任何自排 tick）。
    """
    patched["unread"] = [_envelope("e1", "饭点的香味")]

    fired: list = []

    async def fake_fire(*, lane, persona_id, self_wake):
        fired.append({"self_wake": dict(self_wake)})
        return bool(self_wake)

    monkeypatch.setattr(lw, "fire_life_self_wake", fake_fire)

    def _script_schedule():
        async def _run(tools):
            by_name = {t.name: t for t in tools}
            await by_name["update_life_state"].invoke(
                {"current_state": "起来了", "response_mood": "迷糊", "activity_type": "move"}
            )
            await by_name["schedule"].invoke({"seconds": 600})

        return _run

    _FakeAgent.install(monkeypatch, script=_script_schedule())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(fired) == 1, "event 唤醒一轮收口也要 fire（被起头后自排接力）"
    assert fired[0]["self_wake"].get("delay_ms") == 600_000


# ---------------------------------------------------------------------------
# 阶段 1B Task 3 —— life round marker 幂等（对称 world 的 turn 幂等 round marker）。
#
# life 现在只有单飞锁 + 45s 冷却挡重复唤醒，没有 world 那样的轮幂等。两个唤醒源叠加
# （durable 重投：整轮重试 / delayed trigger 重投；debounce 补敲的 EventArrived，同
# 一批 event → 同 round_id）会让 life 重放出两轮、transcript 重复一轮。意识流落 PG
# durable 之后这个缺陷从"24h 自愈"变永久。Task 3 照搬 world 的 round marker：本轮
# round_id 印进喂给循环的 USER stimulus（写回 transcript），下次同 round_id 重投从
# session 历史查到这行标记 → 跳过（不再 run / 不重复写 transcript / 幂等不破坏已读）。
# ---------------------------------------------------------------------------


def _inmem_transcript_agent(monkeypatch, store, *, script=None):
    """安装一个把本轮传入 messages 追加进 ``store`` 的 FakeAgent（模拟 core 写回）。

    world 的 round marker 测试靠"run 把带本轮标记的 user 消息写进 transcript、重投时
    从历史查到这行标记跳过"验证。life 同构：这里让 fake ``Agent.run`` 把它收到的
    messages 追加进内存 transcript store，配合把节点的 ``load_session`` 指到同一个
    store（见调用处），就能跨两次唤醒模拟"第一轮写回 → 第二轮 load 查重"。

    ``script`` 是 ``async def(tools)`` 回调（模拟模型在循环里调工具，触发 durable
    handler 副作用），与 ``_FakeAgent.install`` 同义；这里自带一个独立类承载它，避免
    ``_FakeAgent.run`` 硬读 ``_FakeAgent._script`` 导致子类脚本失效。
    """
    from app.agent.neutral import Message, Role

    class _PersistingAgent:
        instances: list = []

        def __init__(self, cfg, *, tools=None, **kwargs):
            self.cfg = cfg
            self.tools = tools or []
            self.run_calls: list[dict] = []
            _PersistingAgent.instances.append(self)

        async def run(
            self, messages, *, prompt_vars=None, context=None, session_id=None,
            max_retries=None,
        ):
            self.run_calls.append({"messages": messages, "session_id": session_id})
            store.extend(messages)  # 模拟 core 把本轮传入 messages 写回 transcript
            if script is not None:
                await script(self.tools)
            return Message(role=Role.ASSISTANT, content="ok")

    _PersistingAgent.instances = []
    monkeypatch.setattr(lw, "Agent", _PersistingAgent)
    return _PersistingAgent


@pytest.mark.asyncio
async def test_stimulus_carries_round_marker(patched, monkeypatch):
    """喂给循环的 USER 消息里带本轮 round marker（对称 world，turn 幂等查重靠它）。"""
    patched["unread"] = [_envelope("e1", "水壶在响"), _envelope("e2", "千凪在厨房")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    call = _FakeAgent.last_run()
    round_id = lw._derive_life_round_id(
        lane="coe-t3", persona_id="akao", read_ids=["e1", "e2"],
    )
    blob = "".join(m.text() for m in call["messages"])
    assert lw._round_marker(round_id) in blob, (
        "stimulus 必须带本轮 round marker（turn 幂等查重靠它）"
    )


@pytest.mark.asyncio
async def test_round_already_processed_detects_marker_in_history():
    """_round_already_processed：USER 历史里含本轮标记即判已处理（对称 world）。"""
    from app.agent.neutral import Message, Role

    round_id = "r-xyz"
    marker = lw._round_marker(round_id)
    history_hit = [Message(role=Role.USER, content=f"{marker}\n现在是 20:30。")]
    history_miss = [Message(role=Role.USER, content="现在是 20:30。")]

    assert lw._round_already_processed(history_hit, round_id) is True
    assert lw._round_already_processed(history_miss, round_id) is False


@pytest.mark.asyncio
async def test_dual_source_replay_runs_only_one_round(patched, fake_redis, monkeypatch):
    """命门：同一批唤醒来两次（durable 重投 + debounce 补敲同批 event）→ 只真跑一轮。

    模拟两个唤醒源叠加：第一次唤醒（durable 投递 / debounce 第一敲）把带本轮 round
    marker 的 stimulus 写进 transcript；第二次唤醒（durable 重投 / debounce 补敲同一
    批 event → 同 read_ids → 同 round_id）从 session 历史查到标记 → 被 round marker
    挡掉、跳过：不再 run（不重放 durable 工具）、不重复写 transcript、幂等不破坏已读。

    没有 round marker 时（红）：两次唤醒都会 run（cd 已被清掉、event_ids 相同但无轮
    幂等），断言 run 只 1 次会失败、transcript 出现两份标记。
    """
    # 内存 transcript store：fake Agent.run 把本轮 messages 追加进来（模拟 core 写回），
    # 节点的 load_session 指到同一个 store —— 跨两次唤醒模拟"第一轮写回 → 第二轮查重"。
    store: list = []

    async def _load_from_store(session_id, **kwargs):
        return list(store)

    monkeypatch.setattr(lw, "load_session", _load_from_store)
    _inmem_transcript_agent(monkeypatch, store)

    same_batch = [_envelope("e1", "水壶在响"), _envelope("e2", "千凪在厨房")]
    patched["unread"] = list(same_batch)

    arrived = EventArrived(lane="coe-t3", persona_id="akao")

    # 第一次唤醒：正常跑一轮，stimulus（含 round marker）写进 store。
    await lw.life_wake_node(arrived)

    # 清掉 cd key —— 否则第二次会被 cd reschedule 挡掉，测不到 round marker 这一层。
    await fake_redis.delete(lw._cd_key("coe-t3", "akao"))

    # 第二次唤醒：同一批 event（同 read_ids → 同 round_id）重投。round marker 应挡掉它。
    await lw.life_wake_node(arrived)

    # 只真跑一轮：第二次被 round marker 挡掉、不再 run。
    run_total = sum(len(inst.run_calls) for inst in lw.Agent.instances)
    assert run_total == 1, (
        f"同一批唤醒重投只该真跑一轮（turn 幂等），实际 run {run_total} 次"
    )

    # transcript 不重复一轮：本轮 round marker 只出现一次。
    round_id = lw._derive_life_round_id(
        lane="coe-t3", persona_id="akao", read_ids=["e1", "e2"]
    )
    marker = lw._round_marker(round_id)
    marker_count = sum(1 for m in store if marker in m.text())
    assert marker_count == 1, (
        f"transcript 不该重复一轮（同 round marker 只该一份），实际 {marker_count} 份"
    )

    # 幂等不破坏已读：第二次被挡时不重复标已读（mark 只在真跑的第一轮发生一次）。
    assert patched["marked"] == [["e1", "e2"]], (
        f"被 round marker 挡掉的第二次不该重复标已读，实际 {patched['marked']}"
    )


@pytest.mark.asyncio
async def test_replay_skips_does_not_redo_durable_act(patched, fake_redis, monkeypatch):
    """重投被挡时不重做 durable 工具（act）：act 只在真跑的第一轮发生一次。

    **这测的是 round marker 那一层（必改 3 两层分工的第一层）**：round marker 兜的是
    "Agent.run **成功写回 transcript 后**整轮重投 → load_session 查到标记 → skip"。这里
    fake Agent.run 在 happy path 里把带标记的 stimulus 写进 store（=写回成功），第二次
    重投从历史查到标记、根本不进 run，act handler 自然不被第二次触发。

    它**挡不住**"durable 工具已副作用、Agent.run 未成功写回 transcript（marker 没落）→
    重投"这条——那条没落 marker、load_session 查不到、会再进 run 第二次 perform_act。真正
    兜那条的是 act 的 act_id durable 去重（perform_act → ActPerformed (lane,act_id) 自然键
    幂等），见 test_act_id_durable_dedup_blocks_reinvoke_before_writeback（第二层）。
    """
    store: list = []

    async def _load_from_store(session_id, **kwargs):
        return list(store)

    monkeypatch.setattr(lw, "load_session", _load_from_store)
    _inmem_transcript_agent(
        monkeypatch, store,
        script=_script_update_then_act(description="我去厨房煮咖啡"),
    )

    patched["unread"] = [_envelope("e1", "晨光斜照进房间")]
    arrived = EventArrived(lane="coe-t3", persona_id="akao")

    await lw.life_wake_node(arrived)
    await fake_redis.delete(lw._cd_key("coe-t3", "akao"))
    await lw.life_wake_node(arrived)  # durable 重投同一批

    assert len(patched["acts"]) == 1, (
        f"durable 重投被 round marker 挡掉，act 只该做一次，实际 {len(patched['acts'])} 次"
    )


# ---------------------------------------------------------------------------
# 必改 3 第二层（codex T3）：act_id durable 去重兜"工具副作用后、写回前崩溃"的重投。
#
# round marker（上面那层）只在 Agent.run 成功写回 transcript 后生效。它挡不住"durable
# 工具已副作用（perform_act 写了 ActPerformed）、Agent.run 未成功写回 transcript（marker
# 没落）→ 整轮重投"——这条没落 marker、load_session 查不到、会再进 run 第二次 perform_act。
# 真正兜这条的是 act 的 act_id 幂等：pull 范式下 perform_act → insert_idempotent(ActPerformed)
# 直接落库，按 (lane, act_id) 自然键 ON CONFLICT DO NOTHING。同一批唤醒重投 → 同 round_id →
# 同 act_id → 第二次 perform_act 产同一个 (lane, act_id) → 去重、ActPerformed 只一条不重复。
# 本测从真实 PG 持久化层（insert_idempotent）钉死这层——这正是 perform_act 对同一条
# ActPerformed 重写做的去重动作。
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_act_id_durable_dedup_blocks_reinvoke_before_writeback(test_db):
    """act_id 去重拦住"写回前崩溃 → 重投第二次 perform_act"：同 (lane, act_id) 只落一条。

    模拟 act 已执行（perform_act 写 ActPerformed）但 Agent.run 未写回 transcript（marker
    没落）→ 整轮重投（同 round_id → 同 act_id）→ act 再 perform_act 同 act_id。perform_act
    按 (lane, act_id) 自然键 insert_idempotent，第二次 ON CONFLICT DO NOTHING →
    ActPerformed 只一条不重复（哪怕第二次 description 不同，act_id 同就去重）。
    """
    from app.domain.world_events import ActPerformed
    from app.runtime.persist import insert_idempotent, select_all_versions
    from tests.runtime.conftest import migrate

    await migrate(ActPerformed, test_db)

    lane = "coe-t3"
    # act_id 由本轮稳定派生（重投取同值，不依赖 now / 模型）。
    act_id = "round-derived-act-id"

    # 第一次：act 执行 → perform_act 直接 insert_idempotent 写 ActPerformed。
    n1 = await insert_idempotent(
        ActPerformed(
            lane=lane, act_id=act_id, persona_id="akao",
            description="我去厨房煮咖啡", occurred_at="2026-06-05T21:00:00+08:00",
        )
    )
    assert n1 == 1, "第一次 act 应落一条 ActPerformed"

    # 写回前崩溃 → 整轮重投 → 同 round_id → 同 act_id → 第二次 perform_act 同 (lane, act_id)。
    # （description 故意写不同：act_id 同就去重，去重靠自然键不靠内容。）
    n2 = await insert_idempotent(
        ActPerformed(
            lane=lane, act_id=act_id, persona_id="akao",
            description="重投时模型给了不同措辞", occurred_at="2026-06-05T21:00:01+08:00",
        )
    )
    assert n2 == 0, "重投第二次 perform_act 同 act_id → durable 去重、不再落第二条"

    # PG 里 (lane, act_id) 只有一条，且是第一次那条。
    rows = await select_all_versions(ActPerformed, {"lane": lane, "act_id": act_id})
    assert len(rows) == 1, f"同 (lane, act_id) 只该有一条 ActPerformed，实际 {len(rows)} 条"
    assert rows[0].description == "我去厨房煮咖啡", "保留第一次的 act，不被重投覆盖"


@pytest.mark.integration
async def test_act_tool_derived_id_lands_and_dedups_end_to_end(test_db):
    """端到端串起 build_life_tools 派生 per_act_id → 真实 perform_act/ActPerformed 落库 + 去重。

    采纳补充 #2（codex T3）：现有 test_life_tools 在工具层 mock perform_act 验派生、
    test_act_id_durable_dedup_blocks_reinvoke_before_writeback 单独用真实 PG 验去重，缺
    一条把两端串起来的——即 act 工具自己派生的那个 per_act_id 真落进 PG、且整轮重投
    （重建工具集、重做同序 act）经真实 insert_idempotent 去重只落一条。

    P6 失败重试命门也一并端到端钉死：第一件 act 成功落库后，第二件 act 派生的是不同
    序号的不同 id（同轮两件各自落库）；重投整轮（同 base act_id、同序）→ 同 per_act_id
    → durable 去重 → PG 里每件仍只一条。
    """
    import uuid

    from sqlalchemy import text

    from app.domain.world_events import ActPerformed
    from app.nodes.life_tools import build_life_tools
    from app.runtime.persist import select_all_versions
    from tests.runtime.conftest import migrate

    await migrate(ActPerformed, test_db)

    lane = "coe-t3"
    base_act_id = "round-derived-base-id"
    observed_at = "2026-06-05T21:00:00+08:00"

    # 工具内部派生 per_act_id = uuid5(NAMESPACE_OID, f"{base}:{seq}")；端到端要钉死
    # "工具自己派生的那个 id 真落进 PG"，所以这里照同一口径算出期望 id。
    def _expected_id(seq: int) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{base_act_id}:{seq}"))

    async def _row_count() -> int:
        async with test_db.connect() as conn:
            r = await conn.execute(
                text("SELECT count(*) FROM data_act_performed WHERE lane = :lane"),
                {"lane": lane},
            )
            return r.scalar_one()

    def _act_tool():
        tools = {
            t.name: t
            for t in build_life_tools(
                lane=lane,
                persona_id="akao",
                act_id=base_act_id,
                observed_at=observed_at,
            )
        }
        return tools["act"]

    # 第一轮：同一 base act_id 下真做两件 act（走真实 perform_act → insert_idempotent）。
    act_a = _act_tool()
    assert await act_a.invoke({"description": "我去厨房煮咖啡"}) == "已经做了"
    assert await act_a.invoke({"description": "顺便给千凪带一杯"}) == "已经做了"

    # 工具派生的第 1 / 第 2 件 id 真落进 PG（端到端：派生口径 = 落库 act_id）。
    seq1 = await select_all_versions(
        ActPerformed, {"lane": lane, "act_id": _expected_id(1)}
    )
    seq2 = await select_all_versions(
        ActPerformed, {"lane": lane, "act_id": _expected_id(2)}
    )
    assert [r.description for r in seq1] == ["我去厨房煮咖啡"]
    assert [r.description for r in seq2] == ["顺便给千凪带一杯"]
    assert await _row_count() == 2, "同轮两件 act 应各自落一条（不同序号 → 不同 id）"

    # 重投整轮：重建工具集（同 base act_id）、重做同序两件 → 同 per_act_id → durable 去重。
    act_b = _act_tool()
    assert await act_b.invoke({"description": "我去厨房煮咖啡"}) == "已经做了"
    assert await act_b.invoke({"description": "顺便给千凪带一杯"}) == "已经做了"

    assert await _row_count() == 2, (
        "重投同序两件应被 (lane, act_id) 去重、不新增"
    )
    # 仍是首次那两条（first-landed-wins，不被重投覆盖）。
    rows1 = await select_all_versions(
        ActPerformed, {"lane": lane, "act_id": _expected_id(1)}
    )
    assert [r.description for r in rows1] == ["我去厨房煮咖啡"]


@pytest.mark.integration
async def test_note_tool_derived_id_lands_and_dedups_end_to_end(test_db):
    """端到端串起 note 工具派生 entry_id → 真实 note_entry/NotebookEntry 落库 + 去重。

    对称 act 的端到端去重测试：note 工具自己从 base act_id 派生的 entry_id 真落进 PG，
    整轮重投（重建工具集、重做同序的 note）经真实 insert_idempotent 去重、每件仍只一条。
    这是「记一条」幂等（durable mutation、整轮重试不重复记）的端到端证据。
    """
    import uuid

    from sqlalchemy import text

    from app.domain.notebook import NotebookEntry, list_notebook_entries
    from app.nodes.life_tools import build_life_tools
    from tests.runtime.conftest import migrate

    await migrate(NotebookEntry, test_db)

    lane = "coe-t3"
    base_act_id = "round-derived-base-id"
    observed_at = "2026-06-13T12:30:00+08:00"

    def _expected_id(seq: int) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_OID, f"{base_act_id}:note:{seq}"))

    async def _row_count() -> int:
        async with test_db.connect() as conn:
            r = await conn.execute(
                text("SELECT count(*) FROM data_notebook_entry WHERE lane = :lane"),
                {"lane": lane},
            )
            return r.scalar_one()

    def _note_tool():
        tools = {
            t.name: t
            for t in build_life_tools(
                lane=lane, persona_id="akao",
                act_id=base_act_id, observed_at=observed_at,
            )
        }
        return tools["note"]

    # 第一轮：同一 base act_id 下真记两条（一备忘 + 一日程，走真实 insert_idempotent）。
    note_a = _note_tool()
    out1 = await note_a.invoke({"content": "想看那部动画"})
    out2 = await note_a.invoke(
        {"content": "三点陪我妹", "remind_at": "2026-06-13T15:00:00+08:00"}
    )
    # 出参带各自的 id（spec：记一条出参 = id + 确认）
    assert _expected_id(1) in out1
    assert _expected_id(2) in out2
    assert await _row_count() == 2, "同轮两条 note 应各自落一条（不同序号 → 不同 id）"

    # 落库内容核对：第 2 条带 remind_at（日程）、第 1 条 None（备忘）。
    entries = {
        e.entry_id: e
        for e in await list_notebook_entries(
            lane=lane, persona_id="akao", active_only=False
        )
    }
    assert entries[_expected_id(1)].remind_at is None
    assert entries[_expected_id(2)].remind_at == "2026-06-13T15:00:00+08:00"

    # 重投整轮：重建工具集（同 base act_id）、重做同序两条 → 同 entry_id → durable 去重。
    note_b = _note_tool()
    await note_b.invoke({"content": "想看那部动画"})
    await note_b.invoke(
        {"content": "三点陪我妹", "remind_at": "2026-06-13T15:00:00+08:00"}
    )
    assert await _row_count() == 2, "重投同序两条应被 (lane, persona, entry_id) 去重、不新增"


# ---------------------------------------------------------------------------
# 观测刀：每轮 life 思考的 token 落 durable PG（不依赖会丢的 langfuse）。
#
# life_wake_node 用 collect_usage() 把 Agent.run 包住、run 完拿累计 token 调
# record_round_cost 落库，actor = persona_id。这些测试用 fake agent 在 run 里
# _accumulate_usage（模拟 adapter 在 collector 作用域内记 usage），断言收口确实把
# 累计 token 记了下来（这里 record_round_cost 被打桩成记录调用参数）。
# ---------------------------------------------------------------------------


class _UsageAgent(_FakeAgent):
    """run 时往当前 collector 累加一笔 usage（模拟 adapter 在 collect_usage 作用域内
    经 span.update(usage_details=...) 记 token），用来验证节点确实把 run 包进了
    collect_usage 且收口落库。"""

    _usage = {"input": 30, "output": 10, "total": 40, "cache_read_input_tokens": 5}

    async def run(self, messages, **kwargs):
        from app.agent.trace import _accumulate_usage

        _accumulate_usage(self._usage)
        return await super().run(messages, **kwargs)


@pytest.fixture
def cost_recorded(monkeypatch):
    """打桩 life_wake.record_round_cost，记录每次落库调用的 kwargs（含 usage）。"""
    calls: list[dict] = []

    async def fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(lw, "record_round_cost", fake_record)
    return calls


@pytest.mark.asyncio
async def test_life_round_records_token_cost_with_persona_actor(
    patched, cost_recorded, monkeypatch
):
    """一轮 life 收口把本轮累计 token 落 PG，actor = persona_id、带 collect_usage 累计。"""
    patched["unread"] = [_envelope("e1", "水壶在响")]
    _UsageAgent.install(monkeypatch, script=_script_update())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(cost_recorded) == 1, "应落且只落一条本轮成本记录"
    rec = cost_recorded[0]
    assert rec["lane"] == "coe-t3"
    assert rec["actor"] == "akao", "life 的 actor 必须是 persona_id"
    # collect_usage 累计的本轮 token 原样传给 record_round_cost
    assert rec["usage"]["input"] == 30
    assert rec["usage"]["output"] == 10
    assert rec["usage"]["total"] == 40
    assert rec["usage"]["cache_read_input_tokens"] == 5
    assert rec["usage"]["calls"] == 1, "本轮一次 LLM 调用 → calls=1"
    assert rec["round_id"], "必须带 round_id（与 turn 幂等同源）"
    assert rec["observed_at"], "必须带观测时刻"


@pytest.mark.asyncio
async def test_life_cost_record_failure_does_not_fail_round(
    patched, monkeypatch
):
    """落成本失败必须 best-effort 吞掉，不把一轮真实思考搞成失败（标已读照常发生）。

    打桩真实 ``thinking_cost.record_thinking_tokens`` 抛错（走 record_round_cost 里真正
    的 swallow 路径），而非打桩节点的 record_round_cost —— 这样测的是真实吞错语义。
    """
    import app.domain.thinking_cost as tc

    patched["unread"] = [_envelope("e1", "外面在下雨")]
    _UsageAgent.install(monkeypatch, script=_script_update())

    async def boom_record(**kwargs):
        raise RuntimeError("PG down recording cost")

    monkeypatch.setattr(tc, "record_thinking_tokens", boom_record)

    # 不该抛——成本观测是旁路。
    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    # 收口正常完成：标已读照常（成本失败不影响真实思考收口）。
    assert patched["marked"] == [["e1"]]


# ---------------------------------------------------------------------------
# 世界阶段透传：life 每轮 stimulus 带「你们一家所处的现实阶段」段
# （事故：世界阶段已翻页，她的 life 不读世界文档 → 照 persona 旧设定过日子穿帮。
#  机制：每轮唤醒读 read_world_arc(lane)，有则渲染进 stimulus、空链整段缺席。）
# ---------------------------------------------------------------------------

_ARC_HEADER_MARK = "【你们一家所处的现实阶段】"


def _world_arc(narrative, lane="coe-t3"):
    from app.world.arc import WorldArc

    return WorldArc(
        lane=lane, narrative=narrative, turned_at="2026-06-09T18:00:00+08:00"
    )


@pytest.mark.asyncio
async def test_stimulus_carries_arc_awareness_when_arc_exists(patched, monkeypatch):
    """有世界阶段 → 这一轮 USER stimulus 带阶段段（框架标头 + 阶段全文）。"""
    narrative = "一家人刚搬过来，老二换了新学校，眼下是初夏。"
    patched["arc"] = _world_arc(narrative)
    patched["unread"] = [_envelope("e1", "水壶在响")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    assert _ARC_HEADER_MARK in msg_blob, "有世界阶段时 stimulus 必须带阶段段"
    assert narrative in msg_blob, "阶段全文必须原样进 stimulus"


@pytest.mark.asyncio
async def test_arc_awareness_sits_before_per_round_dynamic_content(
    patched, monkeypatch
):
    """阶段段在稳定前缀区：排在每轮都变的「现在几点」与周遭感知之前（缓存前缀原则）。"""
    patched["arc"] = _world_arc("一家人安顿下来了。")
    patched["unread"] = [
        _envelope("s1", "你在房间里，窗外有蝉鸣", kind=EVENT_KIND_SURROUNDINGS),
        _envelope("e1", "楼下传来碗筷声"),
    ]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    stimulus = _FakeAgent.last_run()["messages"][0].text()
    arc_pos = stimulus.index(_ARC_HEADER_MARK)
    assert arc_pos < stimulus.index("现在是"), (
        "阶段段（天/周级才变）必须排在每轮都变的时刻行之前"
    )
    assert arc_pos < stimulus.index("【此刻你周遭】"), (
        "阶段段必须排在当轮感知（周遭切片）之前"
    )


@pytest.mark.asyncio
async def test_cold_arc_chain_renders_no_section_no_placeholder(patched, monkeypatch):
    """空链（还没人写过世界阶段）→ 整段缺席，绝不塞占位文案。"""
    patched["arc"] = None
    patched["unread"] = [_envelope("e1", "水壶在响")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    assert _ARC_HEADER_MARK not in msg_blob
    assert "现实阶段" not in msg_blob, "空链时不许出现任何阶段占位文案"


@pytest.mark.asyncio
async def test_arc_read_uses_wake_event_lane(patched, monkeypatch):
    """lane 口径与本轮唤醒一致：按 EventArrived.lane 读世界阶段（不发明新口径）。"""
    patched["unread"] = [_envelope("e1", "水壶在响")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert patched["arc_lanes"] == ["coe-t3"]


# ---------------------------------------------------------------------------
# 备忘录 & 日程 第二块（进她脑子 · life 唤醒侧）：每轮 stimulus 带「她本子里还没
# 了结的事」一段。
#
# 她在 life 里自己记下的还活着（active）的条目要进她每轮的输入，让她带着自己惦记的
# 事过日子。只读渲染：进输入的就是她自己没标 done / dropped 的（active_only=True），
# **绝不**按年龄 / 条数 / 过期去筛（那是代码替她决定忘掉什么、违宪）。复用第一块的
# 渲染（render_notebook，单一定义处）。空本子 / 读失败 → 整段缺席不补占位、不炸轮。
# ---------------------------------------------------------------------------

_NOTEBOOK_HEADER_MARK = "【你本子里还没了结的事】"


def _notebook_entry(entry_id, content, *, remind_at=None, status="active"):
    from app.domain.notebook import NotebookEntry

    return NotebookEntry(
        lane="coe-t3",
        persona_id="akao",
        entry_id=entry_id,
        content=content,
        remind_at=remind_at,
        status=status,
        noted_at="2026-06-13T10:00:00+08:00",
    )


@pytest.mark.asyncio
async def test_stimulus_carries_notebook_when_active_entries_exist(patched, monkeypatch):
    """她本子里有还活着的条目 → 这一轮 USER stimulus 带「还没了结的事」段（含条目内容）。"""
    patched["notebook"] = [
        _notebook_entry("n1", "想看那部新动画"),
        _notebook_entry("n2", "下午三点陪我妹去琴行", remind_at="2026-06-13T15:00:00+08:00"),
    ]
    patched["unread"] = [_envelope("e1", "水壶在响")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    assert _NOTEBOOK_HEADER_MARK in msg_blob, "有还活着的条目时必须带本子段"
    assert "想看那部新动画" in msg_blob, "备忘内容必须进 stimulus"
    assert "下午三点陪我妹去琴行" in msg_blob, "日程内容必须进 stimulus"


@pytest.mark.asyncio
async def test_notebook_read_with_active_only_true(patched, monkeypatch):
    """进她输入的是 active_only=True（她自己没标 done/dropped 的），不按年龄/条数/过期筛。"""
    patched["notebook"] = [_notebook_entry("n1", "惦记的事")]
    patched["unread"] = [_envelope("e1", "动静")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert patched["notebook_calls"], "必须读过本子"
    call = patched["notebook_calls"][-1]
    assert call["active_only"] is True, "进她输入的只能是她自己没了结的（active_only=True）"
    assert call["lane"] == "coe-t3"
    assert call["persona_id"] == "akao"


@pytest.mark.asyncio
async def test_empty_notebook_section_absent_no_placeholder(patched, monkeypatch):
    """空本子（没活着的条目）→ 整段缺席、绝不塞占位文案。"""
    patched["notebook"] = []
    patched["unread"] = [_envelope("e1", "水壶在响")]
    _FakeAgent.install(monkeypatch, script=None)

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    assert _NOTEBOOK_HEADER_MARK not in msg_blob
    assert "本子" not in msg_blob, "空本子时不许出现任何本子占位文案"


@pytest.mark.asyncio
async def test_notebook_read_failure_section_absent_round_still_runs(patched, monkeypatch):
    """本子读失败 → 整段缺席但这一轮照常跑（注入是上下文增强、绝不炸唤醒）。"""
    patched["notebook_raises"] = RuntimeError("db down reading notebook")
    patched["unread"] = [_envelope("e1", "门铃响了")]
    _FakeAgent.install(monkeypatch, script=_script_update(current_state="去开门"))

    # 不该抛
    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    msg_blob = "".join(m.text() for m in _FakeAgent.last_run()["messages"])
    assert _NOTEBOOK_HEADER_MARK not in msg_blob, "读失败时本子段缺席"
    assert "门铃响了" in msg_blob, "当轮感知照常"
    # 这一轮照常收口（读失败不影响唤醒主流程）
    assert patched["marked"] == [["e1"]]
    assert [s["current_state"] for s in patched["saved"]] == ["去开门"]


# ---------------------------------------------------------------------------
# 备忘录 & 日程 第三块（日程到点提醒）：ScheduleReminderTick 独立信号 + 到点 gate +
# 到点把她叫醒的 life_schedule_reminder_node。
#
# 调度契约（这块最难，先定死）：**每条日程各挂各的提醒**（像 act / world self-wake
# 各投各的），不动她现有 self-wake（next_wake_at）那套语义——日程是在它旁边新加的
# 一路独立唤醒。每条 note/edit 带 remind_at 时各 emit_delayed 一条携带
# (entry_id, remind_at) 的 ScheduleReminderTick；到期走 gate 读这条 entry 的最新一版：
# 仍 active、仍有 remind_at、且 remind_at == tick 携带值才作数（改期 / 划掉 / 撤时间
# 后旧 tick 携带值对不上 → 判废、不误触发）。放行后 deliver_event 把这条日程投进她
# 自己的信箱（kind=ambient、source=notebook），复用 deliver_event 的 EventArrived
# 敲门把她叫醒（到点把她叫醒的地基），她在常规唤醒里看到这条、自己处理。
# ---------------------------------------------------------------------------


def _due_entry(
    entry_id="n1",
    content="三点陪我妹去琴行",
    *,
    remind_at="2026-06-13T15:00:00+08:00",
    status="active",
    lane="coe-t3",
    persona_id="akao",
):
    from app.domain.notebook import NotebookEntry

    return NotebookEntry(
        lane=lane, persona_id=persona_id, entry_id=entry_id,
        content=content, remind_at=remind_at, status=status,
        noted_at="2026-06-13T12:00:00+08:00",
    )


@pytest.fixture
def reminder_patched(monkeypatch):
    """把 reminder 节点的 IO 依赖换成可观测 fake（读单条 entry + 投信箱）。"""
    state = {
        "entry": None,        # find_notebook_entry 返回（None=不存在）
        "delivered": [],      # deliver_event 收到的 kwargs
        "find_calls": [],     # find_notebook_entry 收到的 kwargs
    }

    async def fake_find(*, lane, persona_id, entry_id):
        state["find_calls"].append(
            {"lane": lane, "persona_id": persona_id, "entry_id": entry_id}
        )
        return state["entry"]

    async def fake_deliver(**kwargs):
        state["delivered"].append(kwargs)
        return 1

    monkeypatch.setattr(lw, "find_notebook_entry", fake_find)
    monkeypatch.setattr(lw, "deliver_event", fake_deliver)
    return state


def test_schedule_reminder_tick_shape():
    """ScheduleReminderTick：lane / persona_id / entry_id 三键 + remind_at；transient。"""
    from app.nodes.life_wake import ScheduleReminderTick
    from app.runtime.data import key_fields

    keys = key_fields(ScheduleReminderTick)
    assert {"lane", "persona_id", "entry_id"} <= set(keys), (
        "ScheduleReminderTick 至少含 (lane, persona_id, entry_id) 键（每条日程各挂各的）"
    )
    tick = ScheduleReminderTick(
        lane="coe-t3", persona_id="akao", entry_id="n1",
        remind_at="2026-06-13T15:00:00+08:00",
    )
    assert tick.remind_at == "2026-06-13T15:00:00+08:00"
    assert getattr(ScheduleReminderTick.Meta, "transient", False) is True


@pytest.mark.asyncio
async def test_reminder_due_active_matching_fires_into_inbox(reminder_patched):
    """到点：entry 仍 active、remind_at 与 tick 携带值一致 → 投进她信箱（把她叫醒）。"""
    from app.nodes.life_wake import ScheduleReminderTick, life_schedule_reminder_node

    reminder_patched["entry"] = _due_entry(remind_at="2026-06-13T15:00:00+08:00")

    await life_schedule_reminder_node(
        ScheduleReminderTick(
            lane="coe-t3", persona_id="akao", entry_id="n1",
            remind_at="2026-06-13T15:00:00+08:00",
        )
    )

    assert len(reminder_patched["delivered"]) == 1, "到点应投一条进她信箱（复用敲门叫醒）"
    d = reminder_patched["delivered"][0]
    assert d["lane"] == "coe-t3"
    assert d["persona_id"] == "akao", "投进她**自己**的信箱"
    assert "三点陪我妹去琴行" in d["summary"], "递到她面前的是这条日程的内容（当场知道是哪条）"


@pytest.mark.asyncio
async def test_reminder_dropped_entry_does_not_fire(reminder_patched):
    """划掉之后（status=dropped）原来那个提醒到达 → 判废，不投、不误触发。"""
    from app.nodes.life_wake import ScheduleReminderTick, life_schedule_reminder_node

    reminder_patched["entry"] = _due_entry(status="dropped")

    await life_schedule_reminder_node(
        ScheduleReminderTick(
            lane="coe-t3", persona_id="akao", entry_id="n1",
            remind_at="2026-06-13T15:00:00+08:00",
        )
    )

    assert reminder_patched["delivered"] == [], "划掉的日程到点不该再提醒"


@pytest.mark.asyncio
async def test_reminder_done_entry_does_not_fire(reminder_patched):
    """已做掉（status=done）→ 判废，不投。"""
    from app.nodes.life_wake import ScheduleReminderTick, life_schedule_reminder_node

    reminder_patched["entry"] = _due_entry(status="done")

    await life_schedule_reminder_node(
        ScheduleReminderTick(
            lane="coe-t3", persona_id="akao", entry_id="n1",
            remind_at="2026-06-13T15:00:00+08:00",
        )
    )

    assert reminder_patched["delivered"] == []


@pytest.mark.asyncio
async def test_reminder_rescheduled_stale_tick_does_not_fire(reminder_patched):
    """改期之后：entry 现在的 remind_at 与旧 tick 携带值对不上 → 判废 stale，不误触发。

    照现有 self-wake 的 stale gate 先例（tick 带的时刻 vs 当前条目对不上判废）：她
    把日程从 15:00 改到 16:00，旧那条 15:00 的提醒到达时 entry.remind_at 已是 16:00，
    旧 tick 携带 15:00 ≠ 16:00 → 判废。16:00 的提醒由 edit 时新挂的那条 tick 负责。
    """
    from app.nodes.life_wake import ScheduleReminderTick, life_schedule_reminder_node

    # entry 现在挂的是改后的 16:00
    reminder_patched["entry"] = _due_entry(remind_at="2026-06-13T16:00:00+08:00")

    # 旧 tick 携带的是改前的 15:00
    await life_schedule_reminder_node(
        ScheduleReminderTick(
            lane="coe-t3", persona_id="akao", entry_id="n1",
            remind_at="2026-06-13T15:00:00+08:00",
        )
    )

    assert reminder_patched["delivered"] == [], "改期后旧提醒携带的时刻对不上、不该误触发"


@pytest.mark.asyncio
async def test_reminder_cleared_time_stale_tick_does_not_fire(reminder_patched):
    """撤掉时间（日程变回备忘，remind_at=None）→ 旧 tick 到达判废，不误触发。"""
    from app.nodes.life_wake import ScheduleReminderTick, life_schedule_reminder_node

    reminder_patched["entry"] = _due_entry(remind_at=None)  # 时间被撤了

    await life_schedule_reminder_node(
        ScheduleReminderTick(
            lane="coe-t3", persona_id="akao", entry_id="n1",
            remind_at="2026-06-13T15:00:00+08:00",
        )
    )

    assert reminder_patched["delivered"] == [], "撤掉时间后旧提醒不该触发"


@pytest.mark.asyncio
async def test_reminder_missing_entry_does_not_fire(reminder_patched):
    """entry 不存在（理论上不该发生）→ 判废，不投、不炸。"""
    from app.nodes.life_wake import ScheduleReminderTick, life_schedule_reminder_node

    reminder_patched["entry"] = None

    await life_schedule_reminder_node(
        ScheduleReminderTick(
            lane="coe-t3", persona_id="akao", entry_id="ghost",
            remind_at="2026-06-13T15:00:00+08:00",
        )
    )

    assert reminder_patched["delivered"] == []


@pytest.mark.asyncio
async def test_reminder_event_id_deterministic_dedups_on_replay(reminder_patched):
    """edge 5（不破幂等）：同一条 reminder 重投，deliver 的 event_id 确定 → 信箱去重。

    durable delayed trigger 重投 / 整轮重试会重放同一条 ScheduleReminderTick；投信箱的
    event_id 从 (lane, persona, entry_id, remind_at) 确定派生，两次重投得同一 id，
    deliver_event 按 (lane, persona, event_id) 幂等去重，不重复叫醒。
    """
    from app.nodes.life_wake import ScheduleReminderTick, life_schedule_reminder_node

    reminder_patched["entry"] = _due_entry(remind_at="2026-06-13T15:00:00+08:00")
    tick = ScheduleReminderTick(
        lane="coe-t3", persona_id="akao", entry_id="n1",
        remind_at="2026-06-13T15:00:00+08:00",
    )

    await life_schedule_reminder_node(tick)
    await life_schedule_reminder_node(tick)

    assert len(reminder_patched["delivered"]) == 2, "节点跑两次（去重在 deliver_event 层）"
    ids = {d["event_id"] for d in reminder_patched["delivered"]}
    assert len(ids) == 1, "同一条 reminder 重投必须派生同一 event_id（信箱据此幂等去重）"


@pytest.mark.asyncio
async def test_reminder_two_near_simultaneous_both_fire(monkeypatch):
    """edge 1（多条几乎同时到点）：两条不同日程各自的 tick 各自投递、互不覆盖。

    每条日程各挂各的提醒（独立 tick、独立 event_id），两条几乎同时到点各走一遍节点、
    各投一条进她信箱 —— 她下一轮一次能看到全部到点的，不互相覆盖、不漏。
    """
    from app.nodes.life_wake import ScheduleReminderTick, life_schedule_reminder_node

    delivered: list[dict] = []

    async def fake_find(*, lane, persona_id, entry_id):
        # 每条 entry 都到点、active、remind_at 匹配。
        return _due_entry(
            entry_id=entry_id,
            content=f"日程-{entry_id}",
            remind_at="2026-06-13T15:00:00+08:00",
        )

    async def fake_deliver(**kwargs):
        delivered.append(kwargs)
        return 1

    monkeypatch.setattr(lw, "find_notebook_entry", fake_find)
    monkeypatch.setattr(lw, "deliver_event", fake_deliver)

    await life_schedule_reminder_node(
        ScheduleReminderTick(
            lane="coe-t3", persona_id="akao", entry_id="n1",
            remind_at="2026-06-13T15:00:00+08:00",
        )
    )
    await life_schedule_reminder_node(
        ScheduleReminderTick(
            lane="coe-t3", persona_id="akao", entry_id="n2",
            remind_at="2026-06-13T15:00:00+08:00",
        )
    )

    assert len(delivered) == 2, "两条同时到点各投一条、不互相覆盖"
    contents = " ".join(d["summary"] for d in delivered)
    assert "日程-n1" in contents and "日程-n2" in contents, "两条到点的她都能看到"
    ids = {d["event_id"] for d in delivered}
    assert len(ids) == 2, "两条不同日程派生不同 event_id（不被去重吞掉一条）"


# --- engine 收口：一轮跑完把本轮 note/edit 排的日程提醒各挂各的（fire_schedule_reminders）---


@pytest.mark.asyncio
async def test_life_round_fires_schedule_reminders_for_noted_schedule(patched, monkeypatch):
    """event 唤醒一轮里她 note 了一条带时间的日程 → 收口 fire_schedule_reminders 带这条。

    note 工具把待挂提醒记进 round-scoped 容器，engine 收口调 fire_schedule_reminders
    （对称 fire_life_self_wake）一次性把本轮排的日程各挂各的。这条验证收口真的把容器
    交给了 fire（容器里有那条带 remind_at 的）。
    """
    import uuid

    patched["unread"] = [_envelope("e1", "想起来要陪我妹")]

    fired: list[dict] = []

    async def fake_fire_reminders(*, lane, persona_id, schedule_reminders):
        fired.append(
            {"lane": lane, "persona_id": persona_id,
             "schedule_reminders": dict(schedule_reminders)}
        )

    monkeypatch.setattr(lw, "fire_schedule_reminders", fake_fire_reminders)

    def _script_note():
        async def _run(tools):
            by_name = {t.name: t for t in tools}
            await by_name["note"].invoke(
                {"content": "三点陪我妹", "remind_at": "2026-06-13T15:00:00+08:00"}
            )

        return _run

    _FakeAgent.install(monkeypatch, script=_script_note())

    await lw.life_wake_node(EventArrived(lane="coe-t3", persona_id="akao"))

    assert len(fired) == 1, "一轮收口必须调 fire_schedule_reminders"
    assert fired[0]["lane"] == "coe-t3"
    assert fired[0]["persona_id"] == "akao"
    # 本轮第一件 note 的 entry_id（base act_id 从 event_ids 派生 + note:1 序号）
    base = str(uuid.uuid5(uuid.NAMESPACE_DNS, "coe-t3:akao:e1"))
    entry_id = str(uuid.uuid5(uuid.NAMESPACE_OID, f"{base}:note:1"))
    assert fired[0]["schedule_reminders"] == {entry_id: "2026-06-13T15:00:00+08:00"}, (
        "收口要把本轮排的日程（entry_id → remind_at）交给 fire"
    )
