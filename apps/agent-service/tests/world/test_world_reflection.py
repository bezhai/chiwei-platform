"""world 反思环节契约 — 翻页能力从续写剥离、归独立的反思（Task 2b）.

续写姿态发现不了「页翻了」（coe 实证：长弧 v1 把过期底色结晶了进去），所以翻页
归一个独立的「反思」环节：**无会话**（每次从证据现判，不背叙事惯性）、每日一次、
对表翻页。这些测试钉死反思环节的机制层硬约束：

  * 工具集物理隔离：反思工具集只含 update_arc（续写无手碰长弧，反思无手碰
    detail / notify / sense / sleep）；
  * 无会话：``Agent.run`` 不传 session_id（不续接 transcript）；
  * durable 副作用边界同续写：``max_retries=1``（update_arc 是 durable 写，
    整轮重放会重放它）；
  * 工具运行契约：context.features 带 world_lane + world_round_id（update_arc
    读 ambient context 取 lane）；
  * 反思成功才落当日标记（mark_arc_reflected），失败不落（同日后续轮自动重试）；
  * fail-open：反思抛错只记 error 日志、绝不向上抛（当轮续写照常）；
  * 输入拼装：所有快照都带时间标注（长弧 turned_at / 此刻叙述 world_time /
    现实此刻+今天日期星期），缺失时如实说缺失，模板不硬编任何剧情事实。

哪页该翻由反思推演自主判断（prompt 层约束粒度），代码里没有翻页检测器 /
频率限制器（赤尾宪法）。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import app.world.reflection as reflection_mod
from app.agent.neutral import Message, Role
from app.world.arc import WorldArc
from app.world.reflection import run_arc_reflection
from app.world.state import WorldState
from app.world.tools import WORLD_REFLECT_TOOLS, update_arc

_CST = timezone(timedelta(hours=8))

_NOW = datetime(2026, 6, 10, 8, 30, 0, tzinfo=_CST)  # 周三


def _snapshot(**kwargs) -> WorldState:
    base = {
        "lane": "coe-t2",
        "world_time": "2026-06-09T23:40:00+08:00",
        "detail": "深夜，屋里只剩冰箱的低鸣。",
    }
    base.update(kwargs)
    return WorldState(**base)


def _arc(**kwargs) -> WorldArc:
    base = {
        "lane": "coe-t2",
        "narrative": "这一页的世界进展。",
        "turned_at": "2026-06-01T10:00:00+08:00",
    }
    base.update(kwargs)
    return WorldArc(**base)


def _materials(**kwargs):
    from app.fetch.materials import DailyMaterials

    base = {
        "lane": "coe-t2",
        "date": "2026-06-10",
        "briefing": "今天广州多云，是普通工作日。",
        "fetched_at": "2026-06-10T06:05:00+08:00",
    }
    base.update(kwargs)
    return DailyMaterials(**base)


@pytest.fixture(autouse=True)
def _stub_reflection_io(monkeypatch):
    """stub 反思环节的 IO（读长弧 / 落标记 / 记成本），专测编排机制，不碰真库。"""
    arc_holder: dict = {"arc": None}

    async def fake_read_world_arc(*, lane):
        return arc_holder["arc"]

    marks: list[dict] = []

    async def fake_mark_arc_reflected(*, lane, date):
        marks.append({"lane": lane, "date": date})

    costs: list[dict] = []

    async def fake_record_round_cost(**kwargs):
        costs.append(kwargs)

    monkeypatch.setattr(reflection_mod, "read_world_arc", fake_read_world_arc)
    monkeypatch.setattr(reflection_mod, "mark_arc_reflected", fake_mark_arc_reflected)
    monkeypatch.setattr(reflection_mod, "record_round_cost", fake_record_round_cost)

    reflection_mod._test_arc_holder = arc_holder  # type: ignore[attr-defined]
    reflection_mod._test_marks = marks  # type: ignore[attr-defined]
    reflection_mod._test_costs = costs  # type: ignore[attr-defined]
    yield


def _mock_run(monkeypatch, *, order: list[str] | None = None):
    """把反思的 ``Agent.run`` 换成记录调用参数的桩，返回 captured。"""
    captured: dict = {}

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        if order is not None:
            order.append("reflect_run")
        captured["messages"] = messages
        captured["context"] = context
        captured["session_id"] = session_id
        captured["max_retries"] = max_retries
        captured["tools"] = self._tools
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(reflection_mod.Agent, "run", fake_run)
    return captured


async def _reflect(**overrides):
    kwargs = {
        "lane": "coe-t2",
        "now": _NOW,
        "snapshot": _snapshot(),
        "materials": None,
        "round_id": "round-abc",
        "trace_session_id": "sess-1",
    }
    kwargs.update(overrides)
    await run_arc_reflection(**kwargs)


# ---------------------------------------------------------------------------
# 工具集物理隔离 + 调用契约
# ---------------------------------------------------------------------------


def test_reflect_toolset_contains_only_update_arc():
    """反思工具集只含 update_arc（翻页归反思独占——工具集物理隔离）。"""
    assert WORLD_REFLECT_TOOLS == [update_arc]


def test_reflect_config_prompt_id_is_world_reflect():
    """反思用独立的 AgentConfig，prompt id 钉为 world_reflect（Task 3 在 langfuse 建它）。"""
    assert reflection_mod._REFLECT_CFG.prompt_id == "world_reflect"


@pytest.mark.asyncio
async def test_reflection_runs_agent_with_reflect_tools_only(monkeypatch):
    """反思的 Agent 调用只拿反思工具集（无手碰 detail / notify / sense / sleep）。"""
    captured = _mock_run(monkeypatch)

    await _reflect()

    assert captured["tools"] == WORLD_REFLECT_TOOLS


@pytest.mark.asyncio
async def test_reflection_is_sessionless(monkeypatch):
    """无会话：run 不传 session_id（不续接 transcript，每次从证据现判）。"""
    captured = _mock_run(monkeypatch)

    await _reflect()

    assert captured["session_id"] is None, (
        "反思必须无会话——run 的 session_id 必须是 None（不背叙事惯性）"
    )


@pytest.mark.asyncio
async def test_reflection_passes_max_retries_one(monkeypatch):
    """durable 副作用边界同续写：max_retries=1（update_arc 是 durable 写不被整轮重放）。"""
    captured = _mock_run(monkeypatch)

    await _reflect()

    assert captured["max_retries"] == 1


@pytest.mark.asyncio
async def test_reflection_context_carries_lane_and_round(monkeypatch):
    """工具运行契约：context.features 带 world_lane + world_round_id（update_arc 读它取 lane）。"""
    captured = _mock_run(monkeypatch)

    await _reflect(lane="coe-t2", round_id="round-xyz")

    feats = captured["context"].features
    assert feats.get("world_lane") == "coe-t2"
    assert feats.get("world_round_id") == "round-xyz"


@pytest.mark.asyncio
async def test_reflection_tags_trace_session_without_transcript(monkeypatch):
    """langfuse 归组用 context.session_id（观测可见），但绝不经 run(session_id=) 续接。"""
    captured = _mock_run(monkeypatch)

    await _reflect(trace_session_id="sess-world-today")

    assert captured["context"].session_id == "sess-world-today"
    assert captured["session_id"] is None


# ---------------------------------------------------------------------------
# 成功落标记 / 失败不落 + fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reflection_success_marks_today(monkeypatch):
    """反思成功 → 落当日标记（lane + 今天 CST 日期）。

    这里的桩 run **没有调任何工具**——「对完表判断没有哪页该翻、不调 update_arc」
    是完全合法的成功（与「durable 写失败」严格区分：后者绝不落标记），同样落
    当日标记、当天不再重跑。
    """
    _mock_run(monkeypatch)

    await _reflect(lane="coe-t2", now=_NOW)

    assert reflection_mod._test_marks == [{"lane": "coe-t2", "date": "2026-06-10"}]


@pytest.mark.asyncio
async def test_reflection_failure_does_not_mark_and_does_not_raise(monkeypatch, caplog):
    """run 抛 → 标记不落（同日后续轮自动重试）、异常不向上抛（fail-open）、记 error 日志。"""

    async def boom_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        raise RuntimeError("model boom during reflection")

    monkeypatch.setattr(reflection_mod.Agent, "run", boom_run)

    with caplog.at_level("ERROR"):
        await _reflect()  # 不抛

    assert reflection_mod._test_marks == [], "反思失败绝不落当日标记（同日后续轮重试）"
    assert any(r.levelname == "ERROR" for r in caplog.records), (
        "fail-open 不是静默吞——必须有 error 日志留痕"
    )


@pytest.mark.asyncio
async def test_reflection_failure_skips_cost_record(monkeypatch):
    """run 抛 → 不落成本记录（与续写失败路径口径一致）。"""

    async def boom_run(self, messages, *, prompt_vars=None, context=None,
                       session_id=None, max_retries=2):
        raise RuntimeError("boom")

    monkeypatch.setattr(reflection_mod.Agent, "run", boom_run)

    await _reflect()

    assert reflection_mod._test_costs == []


@pytest.mark.asyncio
async def test_reflection_success_records_cost_with_reflect_actor(monkeypatch):
    """反思是独立的 LLM 调用，token 同样落 durable PG，actor 与续写区分开。"""
    _mock_run(monkeypatch)

    await _reflect(lane="coe-t2", round_id="round-abc")

    assert len(reflection_mod._test_costs) == 1
    rec = reflection_mod._test_costs[0]
    assert rec["lane"] == "coe-t2"
    assert rec["actor"] == "world_reflect"
    assert rec["round_id"] == "round-abc"


# ---------------------------------------------------------------------------
# durable 写失败 ≠ 成功：write_world_arc 抛错必须让整次反思失败
# （不落标记、同日下一轮重试——区别于「没调 update_arc」的合法成功）
# ---------------------------------------------------------------------------


def _dispatching_run(monkeypatch):
    """fake Agent.run：保真核心循环对 update_arc 的 dispatch 传播路径。

    真核心（``app.agent.core`` 的工具循环 → ``tooling.dispatch`` →
    ``Tool.invoke``）对工具异常不兜底——未包 @tool_error 的工具抛错会炸掉整个
    run。这个桩在 ambient context 里真实 invoke update_arc（异常照实穿透），
    钉死「durable 写失败 → run 失败 → 反思失败」整条链，而不是只测 run 抛错。
    """
    from app.agent.runtime_context import agent_context

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None, max_retries=2
    ):
        with agent_context(context):
            await update_arc.invoke({"narrative": "对完表：这一页翻过去了。"})
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(reflection_mod.Agent, "run", fake_run)


@pytest.mark.asyncio
async def test_reflection_durable_write_failure_not_marked(monkeypatch, caplog):
    """update_arc 落库失败 → 整次反思失败：标记不落、fail-open 不抛、记 error。

    反 bug 钉子：write_world_arc 抛错若被 @tool_error 包成 tool result 字符串，
    Agent.run 正常返回 → 误判成功 → 假成功落标记 → 同日重试被吃掉。
    """
    import app.world.tools as tools_mod

    async def boom_write(*, lane, narrative, turned_at):
        raise RuntimeError("pg down during arc write")

    monkeypatch.setattr(tools_mod, "write_world_arc", boom_write)
    _dispatching_run(monkeypatch)

    with caplog.at_level("ERROR"):
        await _reflect()  # fail-open：不向上抛

    assert reflection_mod._test_marks == [], (
        "durable 写失败的反思绝不算成功——标记不落，同日下一轮才能重试"
    )
    assert any(r.levelname == "ERROR" for r in caplog.records), (
        "durable 写失败必须可见（error 日志留痕），不是静默吞"
    )


@pytest.mark.asyncio
async def test_reflection_durable_write_failure_skips_cost_record(monkeypatch):
    """update_arc 落库失败 → 与 run 抛错同口径：不落成本记录。"""
    import app.world.tools as tools_mod

    async def boom_write(*, lane, narrative, turned_at):
        raise RuntimeError("pg down")

    monkeypatch.setattr(tools_mod, "write_world_arc", boom_write)
    _dispatching_run(monkeypatch)

    await _reflect()

    assert reflection_mod._test_costs == []


@pytest.mark.asyncio
async def test_reflection_retries_same_day_after_write_failure_then_marks(monkeypatch):
    """写库失败那轮不落标记 → 同日下一轮重试（写恢复后）成功落标记。

    「一天的机会不被吞掉」的完整故事：第一轮 durable 写失败（无标记）；同日
    下一轮 engine 因标记 != 今天再次调反思，这次写库恢复 → 成功落标记。
    """
    import app.world.tools as tools_mod

    state = {"healthy": False}
    writes: list[dict] = []

    async def flaky_write(*, lane, narrative, turned_at):
        if not state["healthy"]:
            raise RuntimeError("pg down")
        writes.append({"lane": lane, "narrative": narrative})

    monkeypatch.setattr(tools_mod, "write_world_arc", flaky_write)
    _dispatching_run(monkeypatch)

    await _reflect()  # 第一轮：写库失败 → 不落标记
    assert reflection_mod._test_marks == []

    state["healthy"] = True
    await _reflect()  # 同日下一轮重试：写库恢复 → 成功落标记
    assert reflection_mod._test_marks == [{"lane": "coe-t2", "date": "2026-06-10"}]
    assert len(writes) == 1, "恢复后的那轮真实落了一版长弧"


@pytest.mark.asyncio
async def test_reflection_reads_arc_by_lane(monkeypatch):
    """反思自己按当前 lane 现读长弧（不是用调用方缓存的值）。"""
    reads: list[str] = []

    async def fake_read(*, lane):
        reads.append(lane)
        return None

    monkeypatch.setattr(reflection_mod, "read_world_arc", fake_read)
    _mock_run(monkeypatch)

    await _reflect(lane="coe-t2")

    assert reads == ["coe-t2"]


# ---------------------------------------------------------------------------
# 反思输入拼装：单条 user 消息、所有快照带时间标注、缺失如实说、模板无剧情事实
# ---------------------------------------------------------------------------


def _blob(captured) -> str:
    return "".join(m.text() for m in captured["messages"])


@pytest.mark.asyncio
async def test_reflection_input_is_single_user_message(monkeypatch):
    """反思输入是单条 user 消息（无会话、一次喂全证据）。"""
    captured = _mock_run(monkeypatch)

    await _reflect()

    assert len(captured["messages"]) == 1
    assert captured["messages"][0].role == Role.USER


@pytest.mark.asyncio
async def test_reflection_input_carries_now_and_weekday(monkeypatch):
    """输入含现实此刻 + 今天日期 + 星期（对表的现实锚点）。"""
    captured = _mock_run(monkeypatch)

    await _reflect(now=_NOW)

    blob = _blob(captured)
    assert "2026-06-10T08:30:00+08:00" in blob
    assert "2026-06-10" in blob
    assert "星期三" in blob, "对表必须知道今天星期几（_NOW 是周三）"


@pytest.mark.asyncio
async def test_reflection_input_carries_arc_with_turned_at(monkeypatch):
    """长弧现状带 turned_at 时间标注——反思要能看出手里这版长弧是多久前翻的。"""
    reflection_mod._test_arc_holder["arc"] = _arc(
        narrative="这版长弧的全文内容。", turned_at="2026-06-01T10:00:00+08:00"
    )
    captured = _mock_run(monkeypatch)

    await _reflect()

    blob = _blob(captured)
    assert "这版长弧的全文内容。" in blob
    assert "2026-06-01T10:00:00+08:00" in blob, "长弧必须带 turned_at 时间标注"


@pytest.mark.asyncio
async def test_reflection_input_blank_arc_guides_first_version(monkeypatch):
    """长弧为空 → 如实说明空白、引导用 update_arc 写第一版（冷启动靠 prompt 引导）。"""
    reflection_mod._test_arc_holder["arc"] = None
    captured = _mock_run(monkeypatch)

    await _reflect()

    blob = _blob(captured)
    assert "空白" in blob
    assert "update_arc" in blob


@pytest.mark.asyncio
async def test_reflection_input_carries_detail_with_world_time(monkeypatch):
    """最新 detail 带 world_time 写入时刻标注——看得出这帧画面是多久前画下的。"""
    captured = _mock_run(monkeypatch)

    await _reflect(
        snapshot=_snapshot(
            detail="深夜，屋里只剩冰箱的低鸣。",
            world_time="2026-06-09T23:40:00+08:00",
        )
    )

    blob = _blob(captured)
    assert "深夜，屋里只剩冰箱的低鸣。" in blob
    assert "2026-06-09T23:40:00+08:00" in blob, "detail 必须带 world_time 写入时刻标注"


@pytest.mark.asyncio
async def test_reflection_input_no_snapshot_says_so(monkeypatch):
    """还没有任何世界叙述（冷启动）→ 如实说明，不冒充。"""
    captured = _mock_run(monkeypatch)

    await _reflect(snapshot=None)

    blob = _blob(captured)
    assert ("还没有" in blob) or ("没有任何" in blob)


@pytest.mark.asyncio
async def test_reflection_input_placeholder_empty_detail_says_no_narration(monkeypatch):
    """占位快照（detail 空白、仅承载冷启动反思标记）→ 同冷启动如实说明，不喂空叙述。

    冷启动反思成功落标后、续写在写首版叙述前崩溃，下一轮（跨天）反思读到的就是
    这行占位快照——证据段不能渲染成「（这段叙述写于 ）」+ 空文，要如实说还没有
    世界叙述。
    """
    captured = _mock_run(monkeypatch)

    await _reflect(snapshot=_snapshot(detail="", world_time=""))

    blob = _blob(captured)
    assert "这段叙述写于" not in blob, "占位行无真实写入时刻，不得标注「这段叙述写于」"
    assert ("还没有" in blob) or ("没有任何" in blob)


@pytest.mark.asyncio
async def test_reflection_input_carries_materials_briefing(monkeypatch):
    """今日底料有 → briefing 进输入（底料有以底料为准）。"""
    captured = _mock_run(monkeypatch)

    await _reflect(materials=_materials())

    assert "今天广州多云，是普通工作日。" in _blob(captured)


@pytest.mark.asyncio
async def test_reflection_input_materials_carry_date_and_fetched_at(monkeypatch):
    """底料段带底料自己的 date + fetched_at 时间标注。

    无会话的对表场景里，底料自己的抓取时刻就是证据——反思要能看出「这份底料记的
    是哪一天、什么时候抓的」，与长弧 turned_at / detail 的 world_time 同等待遇
    （所有快照都带时间标注）。用与「今天」不同的 date 构造，钉死标注确实来自底料
    本身、不是输入里碰巧出现的今天日期。
    """
    captured = _mock_run(monkeypatch)

    await _reflect(
        materials=_materials(
            date="2026-06-09",
            fetched_at="2026-06-09T06:05:00+08:00",
            briefing="广州有雷阵雨预警。",
        )
    )

    blob = _blob(captured)
    assert "广州有雷阵雨预警。" in blob
    assert "2026-06-09" in blob, "底料段必须带底料自己的 date 标注"
    assert "2026-06-09T06:05:00+08:00" in blob, "底料段必须带 fetched_at 抓取时刻标注"


@pytest.mark.asyncio
async def test_reflection_input_missing_materials_says_so(monkeypatch):
    """今日底料缺失 → 如实说缺失（不读昨天、不冒充事实）。"""
    captured = _mock_run(monkeypatch)

    await _reflect(materials=None)

    blob = _blob(captured)
    assert ("没" in blob) or ("缺" in blob)
    assert "今天的外部底料" in blob or "底料" in blob


def test_reflect_instruction_has_no_hardcoded_plot_facts():
    """反思指令模板绝不硬编剧情事实（高考 / 角色名 / 日期数字）——宪法。"""
    instruction = reflection_mod.reflect_instruction()
    assert "高考" not in instruction
    assert "放榜" not in instruction
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert name not in instruction, f"反思指令不得硬编剧情事实（角色 {name!r}）"
    assert not any(ch.isdigit() for ch in instruction), (
        "反思指令不得硬编具体日期 / 数字事实"
    )


def test_reflect_instruction_pins_recalibration_posture():
    """反思指令钉对表姿态：对表翻页 + 整篇重写语义 + 禁止叙述场景。"""
    instruction = reflection_mod.reflect_instruction()
    assert "update_arc" in instruction, "代码侧 instruction 是工具语义的权威来源"
    # 对表：放回现实此刻检查长弧还成不成立
    assert "成立" in instruction
    # 整篇重写语义（翻过去的页被取代不是被追加）
    assert "重写" in instruction
    # 禁止叙述场景（那是续写的事）
    assert "叙述" in instruction or "场景" in instruction


@pytest.mark.asyncio
async def test_reflection_template_has_no_plot_facts_beyond_inputs(monkeypatch):
    """模板静态文案无剧情事实：给中性输入，输出里不出现高考 / 角色名。"""
    reflection_mod._test_arc_holder["arc"] = None
    captured = _mock_run(monkeypatch)

    await _reflect(snapshot=None, materials=None)

    blob = _blob(captured)
    assert "高考" not in blob
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert name not in blob
