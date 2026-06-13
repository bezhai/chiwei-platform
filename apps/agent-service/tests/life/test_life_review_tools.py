"""睡前回顾的写入工具契约 — update_day_page / update_relationship_page.

回顾本体（Task 2）跑一个无会话 Agent，以她本人第一人称回看刚结束的生活日，
手里只有这两件写入工具（``LIFE_REVIEW_TOOLS``）。契约照 world 反思工具
（update_arc / update_attention，WorldArc 范式第四、五次复用）：

  * 签名只留**语义参数**（narrative / other_user_id）——lane / persona / 目标
    生活日是机制层的事，从 ambient AgentContext 的 features 读，不让模型填；
  * ``written_at`` 由工具体自填现实 CST（客观时间不让模型编）；
  * **不包 @tool_error**：durable 写失败必须穿透炸掉整次回顾（不落 marker、
    下一班重试），不能被包成 tool result 假成功；
  * 独立工具集 ``LIFE_REVIEW_TOOLS``，与 life 的活工具（update_life_state /
    act / chat / schedule）物理隔离——回顾无手碰活工具、活轮无手碰页。

ambient features key 约定（回顾本体 Task 2 构造 AgentContext 时要塞齐）：
``life_review_lane`` / ``life_review_persona_id`` / ``life_review_target_date``
（常量从 app.life.review_tools 导入，不散落字符串）。
"""

from __future__ import annotations

import pytest

from app.life.pages import (
    DayPage,
    RelationshipPage,
    read_day_page,
    read_relationship_page,
)
from app.life.review_tools import (
    FEATURE_REVIEW_LANE,
    FEATURE_REVIEW_PERSONA,
    FEATURE_REVIEW_SCHEDULE_REMINDERS,
    FEATURE_REVIEW_TARGET_DATE,
    LIFE_REVIEW_TOOLS,
    tidy_notebook_entry,
    update_day_page,
    update_relationship_page,
)
from tests.runtime.conftest import migrate


@pytest.fixture
async def pages_db(test_db):
    await migrate(DayPage, test_db)
    await migrate(RelationshipPage, test_db)
    yield test_db


def _review_ctx(schedule_reminders: dict | None = None):
    """回顾本体的 ambient context：lane / persona / 目标生活日走 features。

    目标生活日（target_date）由调用方按 [04:00, 次日 04:00) 口径算好塞进来
    ——生活日边界是钟的约定（Task 2 的事），工具只忠实用它当 Key。

    ``schedule_reminders`` —— round-scoped 待挂日程提醒容器（备忘录 & 日程 bug 1）：
    回顾里 tidy_notebook_entry 改期 / 设成未来时刻时往里记 entry_id → remind_at，
    回顾本体收口 fire_schedule_reminders 给每条各挂一条 tick（与活轮 edit_note 同款）。
    """
    from app.agent.context import AgentContext

    features = {
        FEATURE_REVIEW_LANE: "coe-t1",
        FEATURE_REVIEW_PERSONA: "akao",
        FEATURE_REVIEW_TARGET_DATE: "2026-06-09",
    }
    if schedule_reminders is not None:
        features[FEATURE_REVIEW_SCHEDULE_REMINDERS] = schedule_reminders
    return AgentContext(features=features)


# ---------------------------------------------------------------------------
# 工具集物理隔离（回顾两件 ≠ life 活工具）
# ---------------------------------------------------------------------------


def test_life_review_tools_is_exactly_the_three_review_tools():
    """回顾工具集 = 昨天页 + 关系页 + 翻清本子三件，不混进任何活工具。

    Block 4：睡前回顾多了「翻本子、清理」这一手——她睡前翻本子时能把做过的标 done、
    过时 / 不想做的标 dropped、还惦记的改时间，所以回顾工具集从两件扩到三件。
    """
    assert LIFE_REVIEW_TOOLS == [
        update_day_page,
        update_relationship_page,
        tidy_notebook_entry,
    ]


def test_life_review_tools_disjoint_from_live_life_tools():
    """与 life 活工具物理隔离：两个工具集的名字零交集（靠隔离不靠嘱咐）。

    活轮的工具（update_life_state / act / chat / schedule / note / edit_note /
    read_notebook）拿不到页的手、回顾的工具拿不到活轮的手——她睡前回看一天用的是另
    一双手。回顾里清本子的 ``tidy_notebook_entry`` 与活轮的 ``edit_note`` 名字不同
    （隔离的工具集各有各的本子动作，底层都落到同一个 ``update_entry``、不重复定义逻辑）。
    """
    from app.nodes.life_tools import build_life_tools

    live_tools = build_life_tools(
        lane="coe-t1",
        persona_id="akao",
        act_id="00000000-0000-0000-0000-000000000000",
        observed_at="2026-06-09T23:40:00+08:00",
        self_wake={},
    )
    live_names = {t.name for t in live_tools}
    review_names = {t.name for t in LIFE_REVIEW_TOOLS}
    assert review_names == {
        "update_day_page",
        "update_relationship_page",
        "tidy_notebook_entry",
    }
    assert not live_names & review_names, "回顾工具绝不出现在活工具集里"


# ---------------------------------------------------------------------------
# update_day_page 契约（ambient 绑定 + 时间自填 + 不包 @tool_error）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_day_page_reads_binding_from_ambient_and_self_fills_time(
    monkeypatch,
):
    """update_day_page 透传 narrative；lane / persona / 生活日从 ambient features
    读、written_at 由工具体自填现实 CST（机制绑定不进签名、时间不让模型编）。"""
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    writes: list[dict] = []

    async def fake_write(*, lane, persona_id, date, narrative, written_at):
        writes.append(
            {
                "lane": lane,
                "persona_id": persona_id,
                "date": date,
                "narrative": narrative,
                "written_at": written_at,
            }
        )

    monkeypatch.setattr(review_mod, "write_day_page", fake_write)

    with agent_context(_review_ctx()):
        await update_day_page.invoke({"narrative": "这一天留在心里的几笔。"})

    assert len(writes) == 1
    w = writes[0]
    assert w["lane"] == "coe-t1"
    assert w["persona_id"] == "akao"
    assert w["date"] == "2026-06-09", "生活日从 ambient features 读，不进工具签名"
    assert w["narrative"] == "这一天留在心里的几笔。"
    # written_at 由工具体自填现实 CST（不让模型编）：非空、带 CST 偏移
    assert w["written_at"]
    assert "+08:00" in w["written_at"]


@pytest.mark.asyncio
async def test_update_day_page_write_failure_propagates(monkeypatch):
    """write_day_page 抛错必须穿透 update_day_page 向上炸（不包 @tool_error）。

    durable 写失败若被包成 tool result 字符串喂回模型，Agent.run 正常返回 →
    回顾误判成功 → 假成功落当日 marker → 下一班（凌晨对账）重试被吃掉。让异常
    照实穿透炸掉整次回顾，由回顾的 fail-open 接住：不落 marker、下一班重跑。
    """
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    async def boom_write(*, lane, persona_id, date, narrative, written_at):
        raise RuntimeError("pg down during day page write")

    monkeypatch.setattr(review_mod, "write_day_page", boom_write)

    with agent_context(_review_ctx()):
        with pytest.raises(RuntimeError, match="pg down during day page write"):
            await update_day_page.invoke({"narrative": "写不进去的几笔。"})


@pytest.mark.asyncio
async def test_update_day_page_missing_binding_fails_fast():
    """ambient features 缺绑定 → LookupError 失败快（暴露 Task 2 的 wiring bug）。

    空 lane / 空 persona / 空生活日落库会写出脏 Key（lane="" 的页永远读不回来），
    比炸掉更糟——宁可整次回顾失败、下一班重试。
    """
    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context

    with agent_context(AgentContext(features={})):
        with pytest.raises(LookupError):
            await update_day_page.invoke({"narrative": "没有绑定就不该写。"})


def test_update_day_page_docstring_pins_few_strokes_semantics():
    """update_day_page 的 docstring（喂给回顾 agent 的工具说明）钉死昨天页语义。

    必须含：① 留下来的几笔、不写流水账；② 整篇重写（同一生活日再写就是新版
    取代旧版，快班写过、对账班重写是常态）。
    """
    doc = update_day_page.definition.description
    assert "几笔" in doc
    assert "流水账" in doc
    assert "重写" in doc
    assert "取代" in doc


# ---------------------------------------------------------------------------
# update_relationship_page 契约（同款 + other_user_id 语义参数）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_relationship_page_passes_target_and_self_fills_time(
    monkeypatch,
):
    """update_relationship_page 透传 other_user_id + narrative；lane / persona 从
    ambient features 读、written_at 自填现实 CST（与 update_day_page 同契约）。"""
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    writes: list[dict] = []

    async def fake_write(*, lane, persona_id, other_user_id, narrative, written_at):
        writes.append(
            {
                "lane": lane,
                "persona_id": persona_id,
                "other_user_id": other_user_id,
                "narrative": narrative,
                "written_at": written_at,
            }
        )

    monkeypatch.setattr(review_mod, "write_relationship_page", fake_write)

    with agent_context(_review_ctx()):
        await update_relationship_page.invoke(
            {"other_user_id": "ou_bezhai", "narrative": "他与我：今天又聊了会儿。"}
        )

    assert len(writes) == 1
    w = writes[0]
    assert w["lane"] == "coe-t1"
    assert w["persona_id"] == "akao"
    assert w["other_user_id"] == "ou_bezhai"
    assert w["narrative"] == "他与我：今天又聊了会儿。"
    assert w["written_at"]
    assert "+08:00" in w["written_at"]


@pytest.mark.asyncio
async def test_update_relationship_page_write_failure_propagates(monkeypatch):
    """write_relationship_page 抛错必须穿透向上炸（不包 @tool_error，理由同昨天页）。"""
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    async def boom_write(*, lane, persona_id, other_user_id, narrative, written_at):
        raise RuntimeError("pg down during relationship page write")

    monkeypatch.setattr(review_mod, "write_relationship_page", boom_write)

    with agent_context(_review_ctx()):
        with pytest.raises(
            RuntimeError, match="pg down during relationship page write"
        ):
            await update_relationship_page.invoke(
                {"other_user_id": "ou_bezhai", "narrative": "写不进去的一页。"}
            )


def test_update_relationship_page_docstring_pins_rewrite_and_size_semantics():
    """update_relationship_page 的 docstring 钉死关系页语义。

    必须含：① 「他与我」（写关系不写档案）；② 整篇重写、新版取代旧版；③ 无删除
    态——淡了就在重写里自然淡；④ 篇幅感——一页之内，旧的让位新的（整篇重写 +
    直接进上下文的页会自然膨胀，纪律在工具说明与 prompt 层钉）。
    """
    doc = update_relationship_page.definition.description
    assert "他与我" in doc
    assert "重写" in doc
    assert "取代" in doc
    assert "淡" in doc
    assert "一页" in doc


# ---------------------------------------------------------------------------
# tidy_notebook_entry 契约（睡前清本子：ambient 绑定 + 复用 update_entry）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tidy_notebook_entry_reads_binding_from_ambient_and_calls_update(
    monkeypatch,
):
    """tidy_notebook_entry 透传 entry_id / 状态 / 改期；lane / persona 从 ambient
    features 读（机制绑定不进签名），底层落到复用的 ``update_entry``（不重写清理逻辑）。
    """
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    calls: list[dict] = []

    async def fake_update_entry(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(review_mod, "update_entry", fake_update_entry)

    with agent_context(_review_ctx()):
        await tidy_notebook_entry.invoke(
            {"entry_id": "e-1", "status": "done"}
        )

    assert len(calls) == 1
    c = calls[0]
    assert c["lane"] == "coe-t1"
    assert c["persona_id"] == "akao"
    assert c["entry_id"] == "e-1"
    assert c["status"] == "done"


@pytest.mark.asyncio
async def test_tidy_notebook_entry_clears_remind_at_on_empty_string(monkeypatch):
    """撤时间走空串信号（与活轮 edit_note 同口径）：'' → clear_remind_at=True、
    底层 update_entry 把日程退回备忘；非空时刻 → 改期；不填 → 不动时间。"""
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    calls: list[dict] = []

    async def fake_update_entry(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(review_mod, "update_entry", fake_update_entry)

    with agent_context(_review_ctx()):
        # 撤时间
        await tidy_notebook_entry.invoke({"entry_id": "e-1", "remind_at": ""})
        # 改期
        await tidy_notebook_entry.invoke(
            {"entry_id": "e-2", "remind_at": "2026-06-15T09:00:00+08:00"}
        )
        # 不动时间（只改内容）
        await tidy_notebook_entry.invoke({"entry_id": "e-3", "content": "改一下"})

    cleared, retimed, untouched = calls
    assert cleared["clear_remind_at"] is True
    assert cleared["remind_at"] is None
    assert retimed["clear_remind_at"] is False
    assert retimed["remind_at"] == "2026-06-15T09:00:00+08:00"
    assert untouched["clear_remind_at"] is False
    assert untouched["remind_at"] is None
    assert untouched["content"] == "改一下"


@pytest.mark.asyncio
async def test_tidy_notebook_entry_missing_binding_fails_fast():
    """ambient features 缺绑定 → LookupError 失败快（同两件页工具，不拿空 Key 写脏）。"""
    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context

    with agent_context(AgentContext(features={})):
        with pytest.raises(LookupError):
            await tidy_notebook_entry.invoke(
                {"entry_id": "e-1", "status": "done"}
            )


@pytest.mark.asyncio
async def test_tidy_notebook_entry_write_failure_propagates(monkeypatch):
    """update_entry 抛错必须穿透向上炸（不包 @tool_error，理由同两件页工具：durable
    清理写失败若被吞成 tool result，回顾会误判成功落 marker、下一班重试被吃掉）。"""
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    async def boom_update(**kwargs):
        raise RuntimeError("pg down during notebook tidy")

    monkeypatch.setattr(review_mod, "update_entry", boom_update)

    with agent_context(_review_ctx()):
        with pytest.raises(RuntimeError, match="pg down during notebook tidy"):
            await tidy_notebook_entry.invoke(
                {"entry_id": "e-1", "status": "dropped"}
            )


def test_tidy_notebook_entry_docstring_pins_tidy_semantics():
    """tidy_notebook_entry 的 docstring（喂给回顾 agent 的工具说明）钉死清本子语义：
    做了标 done / 不做了 dropped / 还惦记改时间——她自己判断，不是代码按规则清。"""
    doc = tidy_notebook_entry.definition.description
    assert "done" in doc
    assert "dropped" in doc


# ---------------------------------------------------------------------------
# bug 1：回顾里改期要挂新 tick（跨块交互）。tidy_notebook_entry 改 / 设成未来提醒
# 时刻时，必须像活轮 edit_note 一样把待挂提醒记进 round-scoped 容器，回顾本体收口
# fire_schedule_reminders 给它挂新 tick——否则旧 tick 被 stale gate 判废、新时刻没
# 有新 tick → 这条日程再也不会提醒（静默失踪）。
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tidy_set_remind_at_records_pending_reminder(monkeypatch):
    """bug 1 复现：回顾里给条目改 / 设成未来时刻 → 记一条待挂提醒（entry_id → remind_at）。

    与活轮 edit_note 的 test_edit_set_remind_at_records_pending_reminder 同款。现状
    tidy 只调 update_entry、不往容器记 → 回顾本体收口时无东西可挂、新时刻没有新 tick。
    """
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    async def fake_update_entry(**kwargs):
        pass

    monkeypatch.setattr(review_mod, "update_entry", fake_update_entry)

    reminders: dict = {}
    with agent_context(_review_ctx(schedule_reminders=reminders)):
        await tidy_notebook_entry.invoke(
            {"entry_id": "n-existing", "remind_at": "2026-06-15T09:00:00+08:00"}
        )

    assert reminders == {"n-existing": "2026-06-15T09:00:00+08:00"}, (
        "回顾里改 / 设成未来时刻要给这条记一条待挂提醒（挂新 tick 的料）"
    )


@pytest.mark.asyncio
async def test_tidy_clear_remind_at_records_none(monkeypatch):
    """回顾里撤掉时间（日程变回备忘）→ 容器记 None（不挂提醒、旧 tick 自然由 gate 判废）。"""
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    async def fake_update_entry(**kwargs):
        pass

    monkeypatch.setattr(review_mod, "update_entry", fake_update_entry)

    reminders: dict = {}
    with agent_context(_review_ctx(schedule_reminders=reminders)):
        await tidy_notebook_entry.invoke({"entry_id": "n1", "remind_at": ""})

    assert reminders.get("n1") is None, "撤时间后这条不该挂新提醒"


@pytest.mark.asyncio
async def test_tidy_done_does_not_record_reminder(monkeypatch):
    """回顾里标 done（了结）→ 不记待挂提醒（了结不需要挂 tick，任务说明钉死）。"""
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    async def fake_update_entry(**kwargs):
        pass

    monkeypatch.setattr(review_mod, "update_entry", fake_update_entry)

    reminders: dict = {}
    with agent_context(_review_ctx(schedule_reminders=reminders)):
        await tidy_notebook_entry.invoke({"entry_id": "n1", "status": "done"})

    assert reminders == {}, "标 done 是了结、不该挂新提醒"


@pytest.mark.asyncio
async def test_tidy_without_reminders_container_still_works(monkeypatch):
    """容器没给（向后兼容 / 旧调用）→ tidy 照常调 update_entry，只是不记待挂提醒。"""
    import app.life.review_tools as review_mod
    from app.agent.runtime_context import agent_context

    calls: list[dict] = []

    async def fake_update_entry(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(review_mod, "update_entry", fake_update_entry)

    # _review_ctx() 不传容器 → features 里没有 FEATURE_REVIEW_SCHEDULE_REMINDERS
    with agent_context(_review_ctx()):
        await tidy_notebook_entry.invoke(
            {"entry_id": "n1", "remind_at": "2026-06-15T09:00:00+08:00"}
        )

    assert len(calls) == 1, "没容器也照常落库（不报错）"


# ---------------------------------------------------------------------------
# 真 PG 端到端（工具真链路：ambient 绑定 → 落库 → 读回）
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_update_day_page_tool_persists_to_pg_and_reads_back(pages_db):
    """工具级真链路：真调 update_day_page → 真 PG 落库 → read_day_page 读回。

    不 mock 持久化：工具体从 ambient features 读 (lane, persona, 生活日) →
    write_day_page 真 insert_append 进 PG → 按同一 Key 读回最新一版。
    """
    from app.agent.runtime_context import agent_context

    narrative = "考完最后一门，晚上家里安静得不真实。"
    with agent_context(_review_ctx()):
        await update_day_page.invoke({"narrative": narrative})

    page = await read_day_page(lane="coe-t1", persona_id="akao", date="2026-06-09")
    assert page is not None
    assert page.narrative == narrative
    assert page.written_at and "+08:00" in page.written_at
    # 泳道隔离：别的 lane 读不到这一页
    assert (
        await read_day_page(lane="prod", persona_id="akao", date="2026-06-09") is None
    )


@pytest.mark.integration
async def test_update_relationship_page_tool_persists_to_pg_and_reads_back(pages_db):
    """工具级真链路：真调 update_relationship_page → 真 PG 落库 → 读回最新一版。"""
    from app.agent.runtime_context import agent_context

    narrative = "他与我：他今天问起考试，我没说太多，他也没追问。"
    with agent_context(_review_ctx()):
        await update_relationship_page.invoke(
            {"other_user_id": "ou_bezhai", "narrative": narrative}
        )

    page = await read_relationship_page(
        lane="coe-t1", persona_id="akao", other_user_id="ou_bezhai"
    )
    assert page is not None
    assert page.narrative == narrative
    assert page.written_at and "+08:00" in page.written_at
    # persona 隔离：同一个真人在别的姐妹那里没有这页
    assert (
        await read_relationship_page(
            lane="coe-t1", persona_id="ayana", other_user_id="ou_bezhai"
        )
        is None
    )


@pytest.mark.integration
async def test_tidy_notebook_entry_tool_marks_entry_done_in_pg(test_db):
    """工具级真链路：先记一条 active 日程 → 回顾里真调 tidy_notebook_entry 标 done →
    读回最新一版状态为 done（睡前清理真落库、复用 update_entry 的版本链）。"""
    from app.agent.runtime_context import agent_context
    from app.domain.notebook import (
        STATUS_DONE,
        NotebookEntry,
        find_notebook_entry,
        note_entry,
    )

    await migrate(NotebookEntry, test_db)

    await note_entry(
        lane="coe-t1",
        persona_id="akao",
        entry_id="e-pg-1",
        content="下午三点陪我妹去琴行",
        remind_at="2026-06-09T15:00:00+08:00",
        noted_at="2026-06-09T10:00:00+08:00",
    )

    with agent_context(_review_ctx()):
        await tidy_notebook_entry.invoke(
            {"entry_id": "e-pg-1", "status": "done"}
        )

    entry = await find_notebook_entry(
        lane="coe-t1", persona_id="akao", entry_id="e-pg-1"
    )
    assert entry is not None
    assert entry.status == STATUS_DONE
    # 内容 / 时间没动（只动状态）、版本链 append 了一版
    assert entry.content == "下午三点陪我妹去琴行"
    assert entry.ver == 1
