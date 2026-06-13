"""睡前回顾本体契约 — 她自己的慢钟（昨天页 + 关系页的写入者）.

回顾照 world 反思（run_arc_reflection）同构：**无会话**（不背当天意识流的叙事
惯性，从证据现判）、单条 user 消息拼证据（全带时间标注、缺失如实说）、fail-open
绝不向上抛、max_retries=1、成功才落 marker、成本落 durable PG。这些测试钉死机制
层硬约束：

  * 工具集物理隔离：只有 LIFE_REVIEW_TOOLS（update_day_page / update_relationship_page）；
  * 无会话：``Agent.run`` 不传 session_id（langfuse 归组走 context.session_id）；
  * ambient 三绑定：context.features 带 lane / persona / target_date 三个 key；
  * 落标核验：run 返回后现读昨天页，**页存在（本次或更早班写的）才落 marker**
    （mark_day_reviewed，已降级为观测留痕、不再当闸读）；run 抛 / 返回了但没写
    页都算失败 → 不落、对账班按页缺失自动补；成本无论成败都记（token 真烧了）；
  * 整段回顾有硬超时（< single_flight TTL），超时走既有 fail-open；
  * durable 写失败 = 整次回顾失败（工具不包 @tool_error，写库失败穿透炸 run）；
  * single_flight 按 (lane, persona, target_date) 包整段——快班与补班撞车时
    冲突方静默让位；
  * 触发源语义（事故修复，2026-06-12 prod）：快班（trigger="sleep"）**无闸、
    每次入睡都跑**，同日后一次整篇盖前一次（版本叠加）；对账班
    （trigger="sweep"）锁内权威复查**按页存在性**——目标日已有页绝不重跑，
    marker 被回笼觉推前也误导不了它；
  * 成本 round_id 从 (lane, persona, target_date, 触发时刻) 派生：同一天多次
    合法回顾各自入账、不被幂等去重吞掉；
  * 证据三态：意识流 / act / 聊天各自有、无两态都如实说，全带时间标注；
  * 空证据护栏：三样证据全空不烧模型（同空信箱 early-return 的机制安全阀）；
  * instruction / 模板零剧情事实（宪法）。

写什么、给谁重写关系页由她自己判断（prompt 层约束），这里没有内容检测器。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import fakeredis.aioredis
import pytest

import app.life.review as review_mod
from app.agent.neutral import Message, Role
from app.agent.trace import make_session_id
from app.data.message_record import CommonMessageRecord
from app.domain.world_events import ActPerformed
from app.life.pages import DayPage, RelationshipPage
from app.life.review import run_day_review
from app.life.review_tools import (
    FEATURE_REVIEW_LANE,
    FEATURE_REVIEW_PERSONA,
    FEATURE_REVIEW_TARGET_DATE,
    LIFE_REVIEW_TOOLS,
)

_CST = timezone(timedelta(hours=8))

# 快班场景：2026-06-10 23:30 她宣布入睡，回看生活日 2026-06-10。
_NOW = datetime(2026, 6, 10, 23, 30, 0, tzinfo=_CST)
_TARGET = "2026-06-10"
_LANE = "coe-t2"
_PERSONA = "akao"


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """single_flight 跑在 in-memory FakeRedis 上（锁竞争是真实的）。"""
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    return fake


def _act(description="我把书桌收拾了一遍", occurred_at="2026-06-10T20:15:00+08:00"):
    return ActPerformed(
        lane=_LANE,
        act_id="act-1",
        persona_id=_PERSONA,
        description=description,
        occurred_at=occurred_at,
    )


def _msg_record(
    *, text, user_id="user-1", username="贝壳", role="user", create_time=1781190000000,
    chat_type="group",
):
    return CommonMessageRecord(
        message_id="m-1",
        user_id=user_id,
        username=username,
        content=json.dumps({"v": 2, "text": text, "items": []}, ensure_ascii=False),
        role=role,
        root_message_id="m-1",
        reply_message_id=None,
        chat_id="chat-a",
        chat_type=chat_type,
        create_time=create_time,
    )


def _chat_block(
    *, chat_name="一家人", entries=None
) -> tuple[str, str | None, list[tuple[CommonMessageRecord, str | None]]]:
    if entries is None:
        entries = [
            (_msg_record(text="赤尾今天有空吗"), None),
            (
                _msg_record(
                    text="有空呀", user_id=None, username=None, role="assistant"
                ),
                _PERSONA,
            ),
        ]
    return ("chat-a", chat_name, entries)


@pytest.fixture(autouse=True)
def stub_io(monkeypatch):
    """stub 回顾本体的全部 IO，专测编排机制，不碰真库。"""
    state = {
        "sessions": {
            make_session_id(_LANE, _PERSONA, "2026-06-10"): [
                Message(role=Role.USER, content="现在是 20:00 CST。窗外有蝉鸣。"),
                Message(role=Role.ASSISTANT, content="今天把欠的练习补完了，心里松了一口气。"),
            ],
            make_session_id(_LANE, _PERSONA, "2026-06-11"): [],
        },
        "acts": [_act()],
        "chats": [_chat_block()],
        "npc_events": [],
        "rel_pages": {},
        "day_page": None,
        "notebook_entries": [],
        "marks": [],
        "costs": [],
        "act_windows": [],
        "chat_windows": [],
        "npc_windows": [],
        "notebook_queries": [],
        "session_loads": [],
        "page_lookups": [],
    }

    async def fake_load_session(session_id):
        state["session_loads"].append(session_id)
        return list(state["sessions"].get(session_id, []))

    async def fake_acts(*, lane, persona_id, start_iso, end_iso):
        state["act_windows"].append(
            {"lane": lane, "persona_id": persona_id, "start": start_iso, "end": end_iso}
        )
        return list(state["acts"])

    async def fake_chats(*, persona_id, since_ms, until_ms, per_chat_limit):
        state["chat_windows"].append(
            {
                "persona_id": persona_id,
                "since_ms": since_ms,
                "until_ms": until_ms,
                "per_chat_limit": per_chat_limit,
            }
        )
        return list(state["chats"])

    async def fake_npc_speech(*, lane, persona_id, start_iso, end_iso):
        state["npc_windows"].append(
            {"lane": lane, "persona_id": persona_id, "start": start_iso, "end": end_iso}
        )
        return list(state["npc_events"])

    async def fake_rel_pages(*, lane, persona_id, other_user_ids):
        state["page_lookups"].append(list(other_user_ids))
        return dict(state["rel_pages"])

    async def fake_day_page(*, lane, persona_id, date):
        return state["day_page"]

    async def fake_day_page_exists(*, lane, persona_id, date):
        # 与 fake_day_page 同一份状态：页存在 = day_page 非 None（对账班闸口径）。
        return state["day_page"] is not None

    async def fake_mark(*, lane, persona_id, date):
        state["marks"].append({"lane": lane, "persona_id": persona_id, "date": date})

    async def fake_cost(**kwargs):
        state["costs"].append(kwargs)

    async def fake_load_persona(persona_id):
        from app.memory._persona import PersonaContext

        return PersonaContext(
            persona_id=persona_id,
            display_name="她自己",
            persona_lite="一段人设",
        )

    async def fake_notebook(*, lane, persona_id, active_only):
        state["notebook_queries"].append(
            {"lane": lane, "persona_id": persona_id, "active_only": active_only}
        )
        return list(state["notebook_entries"])

    monkeypatch.setattr(review_mod, "load_session", fake_load_session)
    monkeypatch.setattr(review_mod, "list_persona_acts_between", fake_acts)
    monkeypatch.setattr(review_mod, "find_persona_spoken_chats_in_window", fake_chats)
    monkeypatch.setattr(
        review_mod, "list_persona_npc_speech_in_window", fake_npc_speech
    )
    monkeypatch.setattr(review_mod, "read_relationship_pages", fake_rel_pages)
    monkeypatch.setattr(review_mod, "read_day_page", fake_day_page)
    monkeypatch.setattr(review_mod, "day_page_exists", fake_day_page_exists)
    monkeypatch.setattr(review_mod, "mark_day_reviewed", fake_mark)
    monkeypatch.setattr(review_mod, "record_round_cost", fake_cost)
    monkeypatch.setattr(review_mod, "load_persona", fake_load_persona)
    monkeypatch.setattr(review_mod, "list_notebook_entries", fake_notebook)
    return state


def _written_page(date=_TARGET) -> DayPage:
    return DayPage(
        lane=_LANE,
        persona_id=_PERSONA,
        date=date,
        narrative="这一天留下来的几笔。",
        written_at=_NOW.isoformat(),
    )


def _mock_run(monkeypatch, stub_io):
    """把 ``Agent.run`` 换成记录参数的桩（模拟成功跑完**且真写了昨天页**），返回 captured。

    落标核验语义：run 返回后回顾本体会现读昨天页，页存在才算成功。这个桩模拟
    模型在工具循环里调了 update_day_page（把页写进 stub 状态），让"成功路径"
    的测试走真实的核验流程。
    """
    captured: dict = {}

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None,
        max_retries=2,
    ):
        captured["messages"] = messages
        captured["prompt_vars"] = prompt_vars
        captured["context"] = context
        captured["session_id"] = session_id
        captured["max_retries"] = max_retries
        captured["tools"] = self._tools
        stub_io["day_page"] = _written_page()
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(review_mod.Agent, "run", fake_run)
    return captured


async def _review(**overrides):
    kwargs = {
        "lane": _LANE,
        "persona_id": _PERSONA,
        "target_date": _TARGET,
        "now": _NOW,
        "trace_session_id": "sess-1",
        "trigger": "sleep",
    }
    kwargs.update(overrides)
    await run_day_review(**kwargs)


def _blob(captured) -> str:
    return "".join(m.text() for m in captured["messages"])


# ---------------------------------------------------------------------------
# 调用契约：工具集隔离 / 无会话 / max_retries / ambient 三绑定 / persona prompt_vars
# ---------------------------------------------------------------------------


def test_review_config_is_life_day_review():
    """独立 AgentConfig：prompt id 钉为 life_day_review（langfuse 主会话发布）。"""
    assert review_mod._REVIEW_CFG.prompt_id == "life_day_review"


@pytest.mark.asyncio
async def test_review_runs_agent_with_review_tools_only(stub_io, monkeypatch):
    """回顾的 Agent 只拿 LIFE_REVIEW_TOOLS（无手碰活轮工具，靠隔离不靠嘱咐）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["tools"] == LIFE_REVIEW_TOOLS


@pytest.mark.asyncio
async def test_review_is_sessionless(stub_io, monkeypatch):
    """无会话：run 不传 session_id——不续接她当天的意识流 transcript。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["session_id"] is None


@pytest.mark.asyncio
async def test_review_passes_max_retries_one(stub_io, monkeypatch):
    """durable 副作用边界：max_retries=1（页写入是 durable 写，不被整轮重放）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["max_retries"] == 1


@pytest.mark.asyncio
async def test_review_context_carries_three_ambient_bindings(stub_io, monkeypatch):
    """工具运行契约：features 塞齐 lane / persona / target_date 三个 key。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    feats = captured["context"].features
    assert feats.get(FEATURE_REVIEW_LANE) == _LANE
    assert feats.get(FEATURE_REVIEW_PERSONA) == _PERSONA
    assert feats.get(FEATURE_REVIEW_TARGET_DATE) == _TARGET


@pytest.mark.asyncio
async def test_review_tags_trace_session_without_transcript(stub_io, monkeypatch):
    """langfuse 归组用 context.session_id（观测标签），绝不经 run(session_id=) 续接。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review(trace_session_id="sess-day")

    assert captured["context"].session_id == "sess-day"
    assert captured["session_id"] is None


@pytest.mark.asyncio
async def test_review_passes_persona_prompt_vars(stub_io, monkeypatch):
    """system prompt 由 langfuse 承载，prompt_vars 契约 = {persona_name, persona_lite}。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["prompt_vars"] == {
        "persona_name": "她自己",
        "persona_lite": "一段人设",
    }


@pytest.mark.asyncio
async def test_review_input_is_single_user_message(stub_io, monkeypatch):
    """回顾输入是单条 user 消息（无会话、一次喂全证据）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert len(captured["messages"]) == 1
    assert captured["messages"][0].role == Role.USER


# ---------------------------------------------------------------------------
# marker：昨天页真写了才落 / 失败不落下一班重试 / 同生活日不重跑
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_success_marks_target_living_day(stub_io, monkeypatch):
    """回顾成功（run 返回 + 现读到昨天页）→ 落目标生活日 marker。"""
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["marks"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "date": _TARGET}
    ]


@pytest.mark.asyncio
async def test_run_returns_but_no_page_written_does_not_mark(
    stub_io, monkeypatch, caplog
):
    """run 正常返回但昨天页没写（模型一个工具没调）→ 本次回顾失败：不落标、
    error 留痕、fail-open 不抛——补班的重试机会不被"空跑成功"烧掉。"""

    async def lazy_run(self, messages, **kwargs):
        return Message(role=Role.ASSISTANT, content="")  # 不写任何页

    monkeypatch.setattr(review_mod.Agent, "run", lazy_run)

    with caplog.at_level("ERROR"):
        await _review()  # 不抛

    assert stub_io["marks"] == [], "没写页的回顾绝不算成功"
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.asyncio
async def test_run_returns_but_no_page_written_still_records_cost(
    stub_io, monkeypatch
):
    """没写页的失败班成本照记（token 真烧了），记账与落标解耦。"""

    async def lazy_run(self, messages, **kwargs):
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(review_mod.Agent, "run", lazy_run)

    await _review()

    assert len(stub_io["costs"]) == 1


@pytest.mark.asyncio
async def test_rerun_counts_page_written_by_earlier_shift(stub_io, monkeypatch):
    """同日重跑：页是更早班写的（上一班写了页但标失败、这次 run 没再写）→
    页存在即算这个生活日有页、照常落标。"""
    stub_io["day_page"] = _written_page()

    async def lazy_run(self, messages, **kwargs):
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(review_mod.Agent, "run", lazy_run)

    await _review()

    assert stub_io["marks"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "date": _TARGET}
    ]


@pytest.mark.asyncio
async def test_review_failure_does_not_mark_and_does_not_raise(stub_io, monkeypatch, caplog):
    """run 抛 → marker 不落（下一班重试）、异常不向上抛（fail-open）、error 留痕。"""

    async def boom_run(self, messages, **kwargs):
        raise RuntimeError("model boom during day review")

    monkeypatch.setattr(review_mod.Agent, "run", boom_run)

    with caplog.at_level("ERROR"):
        await _review()  # 不抛

    assert stub_io["marks"] == []
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.asyncio
async def test_review_failure_skips_cost_record(stub_io, monkeypatch):
    """run 抛 → 不落成本（与反思失败路径同口径）。"""

    async def boom_run(self, messages, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(review_mod.Agent, "run", boom_run)

    await _review()

    assert stub_io["costs"] == []


@pytest.mark.asyncio
async def test_review_cost_records_each_trigger_moment_separately(stub_io, monkeypatch):
    """成本落 durable PG：actor 与 life 本体区分（persona:day_review）；round_id 从
    (lane, persona, target_date, 触发时刻) 派生——同一天多次合法回顾各自入账，
    不被幂等去重吞掉（事故里补班那次成本被吞的修复）。"""
    _mock_run(monkeypatch, stub_io)

    await _review()
    first = stub_io["costs"][0]
    assert first["lane"] == _LANE
    assert first["actor"] == f"{_PERSONA}:day_review"

    # 同 (lane, persona, target, 触发时刻) → 同 round_id（确定性派生、不漂移）。
    await _review()
    second = stub_io["costs"][1]
    assert second["round_id"] == first["round_id"]

    # 同一天、不同触发时刻（清晨回笼觉再睡）→ 不同 round_id：两次都入账。
    await _review(now=datetime(2026, 6, 11, 6, 6, tzinfo=_CST))
    third = stub_io["costs"][2]
    assert third["round_id"] != first["round_id"], "同日两次合法回顾必须各自入账"

    # 不同 target_date → 不同 round_id。
    await _review(target_date="2026-06-11")
    fourth = stub_io["costs"][3]
    assert fourth["round_id"] not in {first["round_id"], third["round_id"]}


# ---------------------------------------------------------------------------
# 触发源语义（事故修复）：快班无闸、对账班按页存在性
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_trigger_runs_even_when_day_already_reviewed(stub_io, monkeypatch):
    """快班无闸：同日已有上一班写的页（起夜 / 回笼觉再睡）→ 仍然跑——后一次回顾
    整篇盖前一次（页版本叠加、读侧取最新版），中间版是设计行为。"""
    stub_io["day_page"] = _written_page()
    captured = _mock_run(monkeypatch, stub_io)

    await _review(trigger="sleep")

    assert "messages" in captured, "快班每次入睡都跑，不被已有页 / marker 挡住"
    assert stub_io["marks"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "date": _TARGET}
    ]
    assert len(stub_io["costs"]) == 1


@pytest.mark.asyncio
async def test_sweep_trigger_skips_when_page_exists(stub_io, monkeypatch):
    """对账班锁内权威复查按页存在性（prod 事故复现）：目标日页已存在 → 绝不重跑
    （不烧模型、不重落标、不记账）——marker 被回笼觉推前也误导不了它。"""
    stub_io["day_page"] = _written_page()
    captured = _mock_run(monkeypatch, stub_io)

    await _review(trigger="sweep")

    assert "messages" not in captured, "已有页的日期对账班绝不重跑"
    assert stub_io["marks"] == []
    assert stub_io["costs"] == []


@pytest.mark.asyncio
async def test_sweep_trigger_runs_when_page_missing(stub_io, monkeypatch):
    """对账班：目标日没有页（昨晚快班没跑成）→ 照常补跑、写页落标。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review(trigger="sweep")

    assert "messages" in captured
    assert stub_io["marks"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "date": _TARGET}
    ]


@pytest.mark.asyncio
async def test_failed_shift_retries_on_next_shift_then_marks(stub_io, monkeypatch):
    """失败不落页的完整故事：快班失败（无页）→ 对账班按页缺失重试成功、写页落标。"""
    calls = {"n": 0}

    async def flaky_run(self, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("first shift boom")
        stub_io["day_page"] = _written_page()  # 这次真写了页
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(review_mod.Agent, "run", flaky_run)

    await _review()  # 快班：失败
    assert stub_io["marks"] == []

    # 凌晨对账班：页仍缺失 → 再跑，这次成功 → 落标。
    await _review(now=datetime(2026, 6, 11, 5, 0, tzinfo=_CST), trigger="sweep")
    assert stub_io["marks"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "date": _TARGET}
    ]


# ---------------------------------------------------------------------------
# durable 写失败 ≠ 成功（工具不包 @tool_error，写库失败穿透炸 run）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durable_write_failure_not_marked(stub_io, monkeypatch, caplog):
    """update_day_page 落库失败 → 整次回顾失败：不落标、fail-open 不抛、error 留痕。"""
    import app.life.review_tools as rt
    from app.agent.runtime_context import agent_context
    from app.life.review_tools import update_day_page

    async def boom_write(**kwargs):
        raise RuntimeError("pg down during day page write")

    monkeypatch.setattr(rt, "write_day_page", boom_write)

    async def dispatching_run(self, messages, *, context=None, **kwargs):
        with agent_context(context):
            await update_day_page.invoke({"narrative": "这一天留下的几笔。"})
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(review_mod.Agent, "run", dispatching_run)

    with caplog.at_level("ERROR"):
        await _review()  # fail-open：不向上抛

    assert stub_io["marks"] == [], "durable 写失败的回顾绝不算成功"
    assert any(r.levelname == "ERROR" for r in caplog.records)


# ---------------------------------------------------------------------------
# 硬超时：整段回顾挂死 → wait_for 掐死、走既有 fail-open
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_review_hard_timeout_fails_open(stub_io, monkeypatch, caplog):
    """整段回顾挂死（run 不返回）→ 硬超时掐死：不落标、error 留痕、不向上抛。"""

    async def hanging_run(self, messages, **kwargs):
        await asyncio.sleep(30)  # 远超（被调小的）超时
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(review_mod.Agent, "run", hanging_run)
    monkeypatch.setattr(review_mod, "DAY_REVIEW_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level("ERROR"):
        await _review()  # 不抛、也绝不真等 30s

    assert stub_io["marks"] == [], "超时的回顾绝不算成功"
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_review_timeout_below_single_flight_ttl():
    """硬超时必须 < 单飞锁 TTL（600s）：锁 TTL 到期后新班能进，挂死的旧班必须
    先被掐死，否则两班并发写同一生活日。"""
    assert (
        review_mod.DAY_REVIEW_TIMEOUT_SECONDS
        < review_mod.DAY_REVIEW_LOCK_TTL_SECONDS
    )


# ---------------------------------------------------------------------------
# single_flight：快班与补班撞车 → 冲突方静默让位
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_flight_conflict_yields_silently(stub_io, monkeypatch, fake_redis, caplog):
    """锁被持有（另一班正在回顾同一 (lane,persona,target)）→ 静默让位：不 run、不抛。"""
    captured = _mock_run(monkeypatch, stub_io)
    await fake_redis.set(
        f"life_day_review:{_LANE}:{_PERSONA}:{_TARGET}", "other-holder"
    )

    with caplog.at_level("INFO"):
        await _review()  # 不抛

    assert "messages" not in captured, "撞锁绝不并发跑第二次回顾"
    assert stub_io["marks"] == []


@pytest.mark.asyncio
async def test_lock_key_scopes_by_target_date(stub_io, monkeypatch, fake_redis):
    """锁按 (lane, persona, target_date) 分键：别的生活日的锁不挡这次回顾。"""
    captured = _mock_run(monkeypatch, stub_io)
    await fake_redis.set(
        f"life_day_review:{_LANE}:{_PERSONA}:2026-06-09", "other-holder"
    )

    await _review()

    assert "messages" in captured, "不同 target_date 的锁不该互相阻塞"


# ---------------------------------------------------------------------------
# 证据拼装：三态（有 / 无）都如实说、全带时间标注、窗口接线正确
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_carries_now_and_target_living_day(stub_io, monkeypatch):
    """输入含现实此刻 + 目标生活日（回看的是哪一天，钟的结论直接给她）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "2026-06-10T23:30:00+08:00" in blob
    assert _TARGET in blob


@pytest.mark.asyncio
async def test_evidence_includes_transcript_with_day_labels(stub_io, monkeypatch):
    """意识流证据：按自然日标注、条目进 blob（窗口跨两个自然日取两天 session）。"""
    stub_io["sessions"][make_session_id(_LANE, _PERSONA, "2026-06-11")] = [
        Message(role=Role.ASSISTANT, content="熬夜想了会儿白天的事。"),
    ]
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "今天把欠的练习补完了" in blob
    assert "熬夜想了会儿白天的事。" in blob
    assert "2026-06-10" in blob and "2026-06-11" in blob


@pytest.mark.asyncio
async def test_evidence_loads_sessions_for_both_natural_days(stub_io, monkeypatch):
    """session 取数合同：load_session 取 target 与 target+1 两个自然日的 session id。"""
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["session_loads"] == [
        make_session_id(_LANE, _PERSONA, "2026-06-10"),
        make_session_id(_LANE, _PERSONA, "2026-06-11"),
    ]


@pytest.mark.asyncio
async def test_evidence_missing_transcript_says_so(stub_io, monkeypatch):
    """意识流缺失 → 如实说没有记录，不冒充。"""
    stub_io["sessions"] = {}
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "没有留下意识流" in _blob(captured)


@pytest.mark.asyncio
async def test_evidence_includes_acts_with_time(stub_io, monkeypatch):
    """act 证据带发生时刻（CST 显示），按这一天她做过的事讲。"""
    stub_io["acts"] = [_act(description="我把阳台的花浇了", occurred_at="2026-06-10T18:05:00+08:00")]
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "我把阳台的花浇了" in blob
    assert "18:05" in blob, "act 必须带发生时刻标注"


@pytest.mark.asyncio
async def test_evidence_missing_acts_says_so(stub_io, monkeypatch):
    """这一天没做过事 → 如实说，不冒充。"""
    stub_io["acts"] = []
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "没有留下你做过事的记录" in _blob(captured)


@pytest.mark.asyncio
async def test_evidence_includes_chats_with_identity(stub_io, monkeypatch):
    """聊天证据：对方原话 + 用户标识 + 名字 + 她自己的话标成"你说"。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "赤尾今天有空吗" in blob
    assert "贝壳" in blob
    assert "user-1" in blob, "关系页要写给 user_id，证据里必须给出这个标识"
    assert "你说" in blob
    assert "有空呀" in blob
    assert "一家人" in blob, "对话归属哪个 chat 要可读"


@pytest.mark.asyncio
async def test_evidence_no_chats_says_so(stub_io, monkeypatch):
    """这一天没聊过天 → 如实说（关系页这次不动的依据）。"""
    stub_io["chats"] = []
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "没有和任何人聊过天" in _blob(captured)


@pytest.mark.asyncio
async def test_old_relationship_pages_fetched_for_chat_partners(stub_io, monkeypatch):
    """旧关系页按「窗口内真实互动过的真人 user_id」取（去重、不含 None / 不含 bot）。"""
    stub_io["chats"] = [
        _chat_block(
            entries=[
                (_msg_record(text="第一句", user_id="user-1", username="贝壳"), None),
                (_msg_record(text="她回了", user_id=None, role="assistant"), _PERSONA),
                (_msg_record(text="第二句", user_id="user-2", username="路人"), None),
                (_msg_record(text="又一句", user_id="user-1", username="贝壳"), None),
            ]
        )
    ]
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["page_lookups"] == [["user-1", "user-2"]]


@pytest.mark.asyncio
async def test_evidence_includes_old_page_with_written_at(stub_io, monkeypatch):
    """有旧页的人：旧页全文 + written_at 时间标注进证据（重写的底稿）。"""
    stub_io["rel_pages"] = {
        "user-1": RelationshipPage(
            lane=_LANE,
            persona_id=_PERSONA,
            other_user_id="user-1",
            narrative="他常来群里找我说话，话头总是接得很自然。",
            written_at="2026-06-09T23:40:00+08:00",
        )
    }
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "他常来群里找我说话" in blob
    assert "2026-06-09T23:40:00+08:00" in blob, "旧关系页必须带 written_at 标注"


@pytest.mark.asyncio
async def test_evidence_first_time_partner_says_no_page(stub_io, monkeypatch):
    """没旧页的人（第一次聊）→ 如实说还没有页，不补占位。"""
    stub_io["rel_pages"] = {}
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "还没有" in _blob(captured)


@pytest.mark.asyncio
async def test_evidence_includes_existing_day_page_on_rerun(stub_io, monkeypatch):
    """同日重跑：已有的昨天页（含 written_at）进证据，重写取代它。"""
    stub_io["day_page"] = DayPage(
        lane=_LANE,
        persona_id=_PERSONA,
        date=_TARGET,
        narrative="入睡前写过的那版：今天最挂心的是没回完的消息。",
        written_at="2026-06-10T23:31:00+08:00",
    )
    # 同日重跑（快班无闸：起夜 / 回笼觉再睡照样跑），旧版进证据、新版整篇盖写
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "入睡前写过的那版" in blob
    assert "2026-06-10T23:31:00+08:00" in blob


@pytest.mark.asyncio
async def test_evidence_no_existing_day_page_says_first_write(stub_io, monkeypatch):
    """这一天还没写过页 → 如实说是第一次写。"""
    stub_io["day_page"] = None
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "还没写过" in _blob(captured)


# ---------------------------------------------------------------------------
# 翻本子 + 清理 + 沉淀（Block 4）：读全部条目进证据、清理工具、instruction 收口
# ---------------------------------------------------------------------------


def _entry(
    *, entry_id, content, status="active", remind_at=None, noted_at="2026-06-10T09:00:00+08:00"
):
    from app.domain.notebook import NotebookEntry

    return NotebookEntry(
        lane=_LANE,
        persona_id=_PERSONA,
        entry_id=entry_id,
        content=content,
        status=status,
        remind_at=remind_at,
        noted_at=noted_at,
    )


@pytest.mark.asyncio
async def test_review_reads_full_notebook_including_done_and_dropped(
    stub_io, monkeypatch
):
    """睡前翻本子读**全部**条目（active_only=False）：含做过 / 划掉 / 陈年过期的，
    她才能在回顾里看到本子全貌、自己处理（不是代码按年龄 / 过期筛）。"""
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["notebook_queries"] == [
        {"lane": _LANE, "persona_id": _PERSONA, "active_only": False}
    ]


@pytest.mark.asyncio
async def test_review_evidence_renders_all_notebook_entries(stub_io, monkeypatch):
    """本子全貌进回顾证据：还惦记的、做过的、过期没处理的都摆在她面前（复用
    render_notebook 渲染，状态标签含派生的「到点了」）。"""
    stub_io["notebook_entries"] = [
        _entry(entry_id="e-active", content="想看那部新动画"),
        _entry(
            entry_id="e-overdue",
            content="下午三点陪我妹去琴行",
            remind_at="2026-06-10T15:00:00+08:00",
        ),
        _entry(entry_id="e-done", content="把欠的练习补完", status="done"),
        _entry(entry_id="e-dropped", content="本来想去逛街", status="dropped"),
    ]
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "想看那部新动画" in blob
    assert "下午三点陪我妹去琴行" in blob
    assert "到点了" in blob, "过期没处理的日程要标「到点了」让她一眼看出"
    assert "把欠的练习补完" in blob and "做了" in blob
    assert "本来想去逛街" in blob and "划了" in blob
    # 条目 id 进证据：她清理 / 改期得指到 id
    assert "e-overdue" in blob


@pytest.mark.asyncio
async def test_review_empty_notebook_says_so(stub_io, monkeypatch):
    """本子空 → 如实说，不冒充（与其它证据三态同口径）。"""
    stub_io["notebook_entries"] = []
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "本子是空的" in _blob(captured)


@pytest.mark.asyncio
async def test_review_tools_include_tidy_notebook(stub_io, monkeypatch):
    """回顾工具集带上清本子的手：她睡前能把做过的标 done、过时的 dropped、还惦记的
    改时间——都落到复用的 update_entry，不重写清理逻辑。"""
    from app.life.review_tools import tidy_notebook_entry

    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    tool_names = {t.name for t in captured["tools"]}
    assert "tidy_notebook_entry" in tool_names
    assert tidy_notebook_entry in captured["tools"]


def test_review_instruction_covers_notebook_tidy_and_sediment():
    """instruction 收口翻本子那一件：回看本子全貌、做过 / 过时 / 不想做的自己清、
    做过的事顺势沉淀进当天的页。零确定性清理规则（清什么全是她判断、宪法）。"""
    instruction = review_mod.review_instruction()
    assert "tidy_notebook_entry" in instruction
    # 清理动作（标 done / dropped / 改时间）由她自己判断
    assert "本子" in instruction
    assert "done" in instruction and "dropped" in instruction
    # 做过的事沉淀进当天的页（复用 update_day_page，不另起一处）
    assert "写一笔" in instruction or "沉淀" in instruction or "写进" in instruction


# ---------------------------------------------------------------------------
# bug 1（跨块交互）：回顾里改期 → 新 tick 真挂上了（不只断言 update_entry 入参）.
#
# 活轮 edit_note 改期会把新提醒记进容器、收口 fire_schedule_reminders 挂新 tick；
# 回顾的 tidy_notebook_entry 之前只调 update_entry、不挂 tick → 她睡前改期后旧 tick
# 被 stale gate 判废、新时刻没有新 tick → 这条日程再也不会提醒。修法：回顾本体也建
# round-scoped 容器、tidy 改期往里记、收口复用 fire_schedule_reminders。
# ---------------------------------------------------------------------------


def _mock_run_invoking_tidy(monkeypatch, stub_io, *, entry_id, remind_at):
    """把 Agent.run 换成「真调一次 tidy_notebook_entry 改期」的桩（绑当轮 context）。

    模拟模型在回顾工具循环里调 tidy_notebook_entry 把某条日程改到一个未来时刻。
    真调工具（在当轮 AgentContext 下）让它真往 round-scoped 容器里记待挂提醒——
    这样收口 fire_schedule_reminders 拿到的就是工具真写下的料，端到端证明新 tick
    会被挂上（不是只断言 update_entry 入参）。底层 update_entry 被 stub 掉（不碰真库）。
    """
    import app.life.review_tools as review_tools_mod
    from app.agent.runtime_context import agent_context
    from app.life.review_tools import tidy_notebook_entry

    async def fake_update_entry(**kwargs):
        pass

    monkeypatch.setattr(review_tools_mod, "update_entry", fake_update_entry)

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None,
        max_retries=2,
    ):
        with agent_context(context):
            await tidy_notebook_entry.invoke(
                {"entry_id": entry_id, "remind_at": remind_at}
            )
        stub_io["day_page"] = _written_page()
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(review_mod.Agent, "run", fake_run)


@pytest.mark.asyncio
async def test_review_reschedule_hangs_new_tick(stub_io, monkeypatch):
    """bug 1：回顾里把一条日程改到未来时刻 → 收口 fire_schedule_reminders 真给它挂新 tick。

    端到端证明（codex 专门点名）：不是只看 update_entry 入参，而是看回顾本体收口时
    真把这条改期的日程交给 fire_schedule_reminders 挂新 tick——否则旧 tick 被判废、
    新时刻没有新 tick、这条日程静默再不提醒。
    """
    fired: list[dict] = []

    async def fake_fire(*, lane, persona_id, schedule_reminders):
        fired.append(
            {
                "lane": lane,
                "persona_id": persona_id,
                "schedule_reminders": dict(schedule_reminders),
            }
        )

    monkeypatch.setattr(review_mod, "fire_schedule_reminders", fake_fire)
    _mock_run_invoking_tidy(
        monkeypatch, stub_io,
        entry_id="e-reschedule", remind_at="2026-06-15T09:00:00+08:00",
    )

    await _review()

    assert len(fired) == 1, "回顾本体收口必须调一次 fire_schedule_reminders"
    f = fired[0]
    assert f["lane"] == _LANE and f["persona_id"] == _PERSONA
    assert f["schedule_reminders"] == {
        "e-reschedule": "2026-06-15T09:00:00+08:00"
    }, "改期的那条日程要被交给收口挂新 tick"


@pytest.mark.asyncio
async def test_review_no_reschedule_fires_empty(stub_io, monkeypatch):
    """回顾里没改任何日程时间（只标 done / 写页）→ 收口容器为空、不挂任何 tick。"""
    fired: list[dict] = []

    async def fake_fire(*, lane, persona_id, schedule_reminders):
        fired.append(dict(schedule_reminders))

    monkeypatch.setattr(review_mod, "fire_schedule_reminders", fake_fire)
    _mock_run(monkeypatch, stub_io)  # 不调 tidy 改期

    await _review()

    assert fired == [{}], "没改期 → 收口容器空、不挂 tick"


@pytest.mark.asyncio
async def test_act_query_receives_living_day_window(stub_io, monkeypatch):
    """act 查询接的是生活日窗口 [target 04:00 CST, 触发时刻]。"""
    _mock_run(monkeypatch, stub_io)

    await _review()

    win = stub_io["act_windows"][0]
    assert win["lane"] == _LANE and win["persona_id"] == _PERSONA
    assert win["start"] == "2026-06-10T04:00:00+08:00"
    assert win["end"] == _NOW.isoformat()


@pytest.mark.asyncio
async def test_chat_query_receives_ms_window_and_limit(stub_io, monkeypatch):
    """聊天查询接毫秒窗口（同一真实时刻换算）+ 参数化的每 chat 条目上限。"""
    _mock_run(monkeypatch, stub_io)

    await _review()

    win = stub_io["chat_windows"][0]
    start = datetime(2026, 6, 10, 4, 0, tzinfo=_CST)
    assert win["persona_id"] == _PERSONA
    assert win["since_ms"] == int(start.timestamp() * 1000)
    assert win["until_ms"] == int(_NOW.timestamp() * 1000)
    assert win["per_chat_limit"] == review_mod.PER_CHAT_MESSAGE_LIMIT


# ---------------------------------------------------------------------------
# 空证据护栏：三样全空不烧模型（机制安全阀，同空信箱 early-return）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_evidence_skips_model_and_marker(stub_io, monkeypatch):
    """意识流 / act / 聊天全空 → 不 run、不落标（这一天没有可回看的经历）。"""
    stub_io["sessions"] = {}
    stub_io["acts"] = []
    stub_io["chats"] = []
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "messages" not in captured
    assert stub_io["marks"] == []
    assert stub_io["costs"] == []


# ---------------------------------------------------------------------------
# 著作权纪律在 prompt 层：instruction 钉姿态、零剧情事实
# ---------------------------------------------------------------------------


def test_review_instruction_pins_writing_discipline():
    """instruction 承载工具语义与任务：第一人称回看 / 几笔不流水账 / 关系页整篇
    重写一页内 / 没聊过不动 / 不编。"""
    instruction = review_mod.review_instruction()
    assert "update_day_page" in instruction
    assert "update_relationship_page" in instruction
    assert "流水账" in instruction
    assert "重写" in instruction
    assert "一页" in instruction
    assert "没" in instruction and "聊" in instruction  # 没聊过天就不动关系页
    assert "编" in instruction  # 不许编


def test_review_instruction_has_no_hardcoded_plot_facts():
    """instruction 模板零剧情事实（高考 / 角色名 / 日期数字）——宪法。"""
    instruction = review_mod.review_instruction()
    assert "高考" not in instruction
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert name not in instruction
    assert not any(ch.isdigit() for ch in instruction)


def test_review_instruction_covers_npc_relationship_pages():
    """第三刀 prompt 收口：关系页那句要从「只认真人」扩到也涵盖来访过的 NPC。

    第四刀（代码层）已让回顾把 NPC 来访摆进证据、给出 ``npc:名字`` 机读键并读回旧
    NPC 关系页。但任务指令那句若还只说「真正聊过天的每个真人」，模型可能只给真人
    写关系页、把证据里的 NPC 互动晾着——NPC 关系跨天长不起来。所以 instruction 必须：

      * 提到来找过她的 NPC（证据里【这一天来找过你的人】那节）也照样写 / 更新关系页；
      * 明确 NPC 那页的 other_user_id 就用证据里给出的 ``npc:名字`` 键；
      * 保持现有克制：没来往过的人（真人或 NPC）一律不动关系页。
    """
    instruction = review_mod.review_instruction()
    # NPC 也要写关系页（指令不能只认真人）
    assert "NPC" in instruction, "关系页指令应扩到涵盖来访过的 NPC"
    assert "来找过" in instruction, (
        "应引用证据里【这一天来找过你的人】那节的来访 NPC"
    )
    # NPC 那页的 other_user_id 用 npc:名字 机读键
    assert "npc:" in instruction, "NPC 关系页的 other_user_id 应用 npc:名字 机读键"
    # 仍守克制：没来往就不动（真人或 NPC 一律）
    assert "没" in instruction and ("来往" in instruction or "聊" in instruction), (
        "应保持「没来往过就不动关系页」的克制（真人或 NPC 一律）"
    )


@pytest.mark.asyncio
async def test_template_has_no_plot_facts_beyond_inputs(stub_io, monkeypatch):
    """模板静态文案零剧情事实：中性输入下 blob 不出现高考 / 角色名。"""
    stub_io["sessions"] = {
        make_session_id(_LANE, _PERSONA, "2026-06-10"): [
            Message(role=Role.ASSISTANT, content="过了平常的一天。"),
        ],
    }
    stub_io["acts"] = []
    stub_io["chats"] = []
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "高考" not in blob
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "ayana"):
        assert name not in blob
