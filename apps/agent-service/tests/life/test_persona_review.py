"""persona review 本体契约 — 周级慢钟（「她的经历慢慢长进她是谁」的写入者）.

照睡前回顾（run_day_review）同构：**无会话**（不续接意识流，从证据现判）、单条
user 消息拼证据（缺失如实说）、fail-open 绝不向上抛、max_retries=1、成本独立
actor、durable 写工具不包 @tool_error。这些测试钉死机制层硬约束：

  * 幂等：锁内复查本周（自然周一 00:00 CST 起）是否已有 **source='review'** 的
    版本——已有则早退（不 seed、不烧模型、不记账）；owner 盖版**不挡班**；
  * seed 在锁内先行：agent run 之前先幂等灌 v0（链非空零操作）——只落 v0 后
    agent 失败＝review 未完成，下一班续补 v1（spec 决策 2）；
  * 证据游标：窗口 = written_at 晚于**上一条 review 版本时点**的日页；owner 版本
    不动游标；首跑（无 review 版）游标 None = 全部现存页；
  * 空窗口护栏：游标之后没有任何新日页 → 不烧模型（同空信箱 early-return）；
  * 核验：run 返回后复查本周 review 版本真落了才算成功——模型一个工具没调 =
    失败，error 留痕、下一班自动补；
  * 成本：actor = ``{persona}:persona_review``，round_id 从 (lane, persona,
    触发时刻) 派生（同刻确定、不同刻各自入账）；
  * durable 写失败穿透炸 run（工具不包 @tool_error）→ fail-open 下一班补；
  * single_flight 按 (lane, persona) 包整段；整段硬超时 < 锁 TTL；
  * instruction / 模板零剧情事实（宪法）。

写什么、改不改由她的传记作者自己判断（纪律在 prompt 层），这里没有内容检测器。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import fakeredis.aioredis
import pytest

import app.life.persona_review as pr_mod
from app.agent.neutral import Message, Role
from app.agent.trace import make_session_id
from app.life.pages import DayPage, RelationshipPage
from app.life.persona_chain import week_start_cst
from app.life.persona_review import run_persona_review
from app.life.persona_review_tools import (
    FEATURE_PERSONA_REVIEW_LANE,
    FEATURE_PERSONA_REVIEW_PERSONA,
    PERSONA_REVIEW_TOOLS,
)

_CST = timezone(timedelta(hours=8))

# 周三 11:00 触发（每日补班 cron 的时刻），本周一 = 2026-06-08 00:00 CST。
_NOW = datetime(2026, 6, 10, 11, 0, 0, tzinfo=_CST)
_LANE = "coe-t2"
_PERSONA = "akao"


@pytest.fixture(autouse=True)
def fake_redis(monkeypatch):
    """single_flight 跑在 in-memory FakeRedis 上（锁竞争是真实的）。"""
    import app.infra.redis as redis_mod

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_mod, "_redis", fake)
    return fake


def _day_page(date="2026-06-09", narrative="考完最后一门，腿是软的。",
              written_at="2026-06-10T05:00:00+08:00") -> DayPage:
    return DayPage(
        lane=_LANE, persona_id=_PERSONA, date=date,
        narrative=narrative, written_at=written_at,
    )


def _rel_page(other_user_id="user-1",
              narrative="他常来群里找我说话，话头接得很自然。",
              written_at="2026-06-09T23:40:00+08:00") -> RelationshipPage:
    return RelationshipPage(
        lane=_LANE, persona_id=_PERSONA, other_user_id=other_user_id,
        narrative=narrative, written_at=written_at,
    )


@pytest.fixture(autouse=True)
def stub_io(monkeypatch):
    """stub review 本体的全部 IO，专测编排机制，不碰真库。

    ``versions`` 是版本链的 in-memory 影子：fake 的读 b / 读 c 实现与
    persona_chain 同语义（只认 source='review'），让「owner 不挡班 / 不动游标」
    在编排层也走真实的过滤逻辑。
    """
    state = {
        "versions": [],  # [{source, written_at, narrative}]
        "seed_calls": [],
        "events": [],    # 顺序记录：seed / run（钉 seed 先于 agent run）
        "day_pages": [_day_page()],
        "rel_pages": [_rel_page()],
        "arc": SimpleNamespace(narrative="一家人刚搬进新的城市，各自适应着。"),
        "persona_row": SimpleNamespace(
            display_name="她自己", persona_lite="出厂身份正文：她是她。"
        ),
        "costs": [],
        "window_calls": [],  # read_day_pages_written_after 收到的游标
        "pushes": [],        # push_persona_diff 收到的 kwargs（Task 3 接线点）
    }

    def _review_versions():
        return [v for v in state["versions"] if v["source"] == "review"]

    async def fake_has_review_this_week(*, lane, persona_id, now=None):
        revs = _review_versions()
        if not revs:
            return False
        written = datetime.fromisoformat(revs[-1]["written_at"])
        return written >= week_start_cst(now)

    async def fake_latest_review_written_at(*, lane, persona_id):
        revs = _review_versions()
        return revs[-1]["written_at"] if revs else None

    async def fake_seed(*, lane, persona_id):
        state["seed_calls"].append({"lane": lane, "persona_id": persona_id})
        state["events"].append("seed")
        if state["versions"]:
            return False
        state["versions"].append(
            {
                "source": "seed",
                "written_at": _NOW.isoformat(),
                "narrative": state["persona_row"].persona_lite,
            }
        )
        return True

    async def fake_latest_version(*, lane, persona_id):
        if not state["versions"]:
            return None
        v = state["versions"][-1]
        return SimpleNamespace(
            narrative=v["narrative"], source=v["source"],
            written_at=v["written_at"], version=len(state["versions"]),
        )

    async def fake_pages_after(*, lane, persona_id, written_after):
        state["window_calls"].append(written_after)
        return list(state["day_pages"])

    async def fake_rel_pages(*, lane, persona_id):
        return list(state["rel_pages"])

    async def fake_arc(*, lane):
        return state["arc"]

    async def fake_find_persona(persona_id):
        return state["persona_row"]

    async def fake_cost(**kwargs):
        state["costs"].append(kwargs)

    async def fake_push(**kwargs):
        state["pushes"].append(kwargs)
        state["events"].append("push")

    # raising=False：红阶段（接线未实现、pr_mod 还没有 push_persona_diff 属性）
    # 旧测试照常跑，只有新接线测试红。
    monkeypatch.setattr(pr_mod, "push_persona_diff", fake_push, raising=False)
    monkeypatch.setattr(pr_mod, "has_review_version_this_week", fake_has_review_this_week)
    monkeypatch.setattr(pr_mod, "read_latest_review_written_at", fake_latest_review_written_at)
    monkeypatch.setattr(pr_mod, "seed_persona_chain", fake_seed)
    monkeypatch.setattr(pr_mod, "read_latest_persona_version", fake_latest_version)
    monkeypatch.setattr(pr_mod, "read_day_pages_written_after", fake_pages_after)
    monkeypatch.setattr(pr_mod, "list_relationship_pages", fake_rel_pages)
    monkeypatch.setattr(pr_mod, "read_world_arc", fake_arc)
    monkeypatch.setattr(pr_mod, "find_persona", fake_find_persona)
    monkeypatch.setattr(pr_mod, "record_round_cost", fake_cost)
    return state


def _mock_run(monkeypatch, stub_io):
    """把 ``Agent.run`` 换成记录参数的桩（模拟成功跑完**且真落了一版 review**）。

    核验语义：run 返回后本体会复查本周 review 版本真落了才算成功。这个桩模拟
    模型在工具循环里调了 update_persona（往版本链影子里 append 一版 review），
    让"成功路径"的测试走真实的核验流程。
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
        stub_io["events"].append("run")
        stub_io["versions"].append(
            {
                "source": "review",
                "written_at": _NOW.isoformat(),
                "narrative": "慢漂后的身份正文。",
            }
        )
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(pr_mod.Agent, "run", fake_run)
    return captured


async def _review(**overrides):
    kwargs = {"lane": _LANE, "persona_id": _PERSONA, "now": _NOW}
    kwargs.update(overrides)
    await run_persona_review(**kwargs)


def _blob(captured) -> str:
    return "".join(m.text() for m in captured["messages"])


def _sources(stub_io) -> list[str]:
    return [v["source"] for v in stub_io["versions"]]


# ---------------------------------------------------------------------------
# 调用契约：工具集隔离 / 无会话 / max_retries / ambient 绑定 / prompt_vars
# ---------------------------------------------------------------------------


def test_persona_review_config_prompt_id():
    """独立 AgentConfig：prompt id 钉为 persona_review（langfuse 主会话发布）。"""
    assert pr_mod._PERSONA_REVIEW_CFG.prompt_id == "persona_review"


@pytest.mark.asyncio
async def test_review_runs_agent_with_persona_review_tools_only(stub_io, monkeypatch):
    """review 的 Agent 只拿 PERSONA_REVIEW_TOOLS 一件（靠隔离不靠嘱咐）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["tools"] == PERSONA_REVIEW_TOOLS
    assert len(PERSONA_REVIEW_TOOLS) == 1, "persona review 只有 update_persona 一件工具"


@pytest.mark.asyncio
async def test_review_is_sessionless(stub_io, monkeypatch):
    """无会话：run 不传 session_id——不续接她的意识流 transcript。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["session_id"] is None


@pytest.mark.asyncio
async def test_review_passes_max_retries_one(stub_io, monkeypatch):
    """durable 副作用边界：max_retries=1（update_persona 是 durable 写，不被整轮重放）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["max_retries"] == 1


@pytest.mark.asyncio
async def test_review_context_carries_ambient_bindings(stub_io, monkeypatch):
    """工具运行契约：features 塞齐 lane / persona 两个 key。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    feats = captured["context"].features
    assert feats.get(FEATURE_PERSONA_REVIEW_LANE) == _LANE
    assert feats.get(FEATURE_PERSONA_REVIEW_PERSONA) == _PERSONA


@pytest.mark.asyncio
async def test_review_tags_trace_session_without_transcript(stub_io, monkeypatch):
    """langfuse 归组：context.session_id = 她当天的意识流 session id（观测标签），
    绝不经 run(session_id=) 续接。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["context"].session_id == make_session_id(
        _LANE, _PERSONA, "2026-06-10"
    )
    assert captured["session_id"] is None


@pytest.mark.asyncio
async def test_review_prompt_vars_contract(stub_io, monkeypatch):
    """prompt_vars 契约 = {persona_name, current_persona}；current_persona 是链上
    最新一版正文（seed 后链非空，注入的是链文本不是主表快照）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert captured["prompt_vars"] == {
        "persona_name": "她自己",
        "current_persona": "出厂身份正文：她是她。",
    }


@pytest.mark.asyncio
async def test_review_input_is_single_user_message(stub_io, monkeypatch):
    """review 输入是单条 user 消息（无会话、一次喂全证据）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert len(captured["messages"]) == 1
    assert captured["messages"][0].role == Role.USER


# ---------------------------------------------------------------------------
# 幂等：本周已有 review 版早退；owner 盖版不挡班
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_skips_when_review_version_exists_this_week(stub_io, monkeypatch):
    """锁内幂等复查：本周已有 review 版本 → 早退（不 seed、不烧模型、不记账）。"""
    stub_io["versions"].append(
        {"source": "review", "written_at": "2026-06-08T11:00:00+08:00",
         "narrative": "本周已慢漂过的一版。"}
    )
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "messages" not in captured, "本周班已完成，今天不跑"
    assert stub_io["seed_calls"] == []
    assert stub_io["costs"] == []


@pytest.mark.asyncio
async def test_owner_version_this_week_does_not_block(stub_io, monkeypatch):
    """bezhai 本周人工盖版（owner）不挡自动班：照常跑（spec 决策 2）。"""
    stub_io["versions"].append(
        {"source": "owner", "written_at": "2026-06-09T09:00:00+08:00",
         "narrative": "bezhai 盖的版。"}
    )
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "messages" in captured, "owner 版本绝不挡当周自动班"


# ---------------------------------------------------------------------------
# seed 在锁内先行；只落 v0 后失败下一班续补 v1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_runs_once_before_agent(stub_io, monkeypatch):
    """seed 在同一锁内、agent run 之前先行（幂等灌 v0），一次 review 只跑一次。"""
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["seed_calls"] == [{"lane": _LANE, "persona_id": _PERSONA}]
    assert stub_io["events"] == ["seed", "run", "push"], (
        "v0 必须先于 agent run 落定（push 是核验成功后的收尾）"
    )


@pytest.mark.asyncio
async def test_agent_failure_after_seed_leaves_v0_then_next_shift_appends_v1(
    stub_io, monkeypatch, caplog
):
    """spec 决策 2 的完整故事：首跑 seed 落了 v0、agent 失败 → review 未完成
    （fail-open 不抛、error 留痕）；下一班幂等复查仍 False → seed 零操作、
    agent 成功 → 链上 v0(seed)+v1(review)。"""
    calls = {"n": 0}

    async def flaky_run(self, messages, **kwargs):
        calls["n"] += 1
        stub_io["events"].append("run")
        if calls["n"] == 1:
            raise RuntimeError("first shift boom")
        stub_io["versions"].append(
            {"source": "review", "written_at": _NOW.isoformat(),
             "narrative": "续补的 v1。"}
        )
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(pr_mod.Agent, "run", flaky_run)

    with caplog.at_level("ERROR"):
        await _review()  # 首班：seed 落 v0、agent 炸
    assert _sources(stub_io) == ["seed"], "只落 v0 = review 未完成"
    assert any(r.levelname == "ERROR" for r in caplog.records)

    await _review(now=_NOW + timedelta(days=1))  # 次日补班（同一周）
    assert _sources(stub_io) == ["seed", "review"], "下一班续补 v1"
    assert len(stub_io["seed_calls"]) == 2, "seed 每班都先行（幂等，第二次零操作）"


@pytest.mark.asyncio
async def test_seed_failure_fails_open(stub_io, monkeypatch, caplog):
    """seed 自身失败（bot_persona 无行等）→ fail-open：不烧模型、不抛、error 留痕。"""

    async def boom_seed(*, lane, persona_id):
        raise ValueError("bot_persona has no row")

    monkeypatch.setattr(pr_mod, "seed_persona_chain", boom_seed)
    captured = _mock_run(monkeypatch, stub_io)

    with caplog.at_level("ERROR"):
        await _review()  # 不抛

    assert "messages" not in captured
    assert any(r.levelname == "ERROR" for r in caplog.records)


# ---------------------------------------------------------------------------
# 失败不落版下一班补；核验：run 返回 ≠ 成功
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_failure_does_not_raise_and_skips_cost(stub_io, monkeypatch, caplog):
    """run 抛 → fail-open 不向上抛、error 留痕、不记成本（与睡前回顾同口径）。"""

    async def boom_run(self, messages, **kwargs):
        raise RuntimeError("model boom during persona review")

    monkeypatch.setattr(pr_mod.Agent, "run", boom_run)

    with caplog.at_level("ERROR"):
        await _review()  # 不抛

    assert _sources(stub_io) == ["seed"], "失败不落 review 版"
    assert stub_io["costs"] == []
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.asyncio
async def test_run_returns_but_no_version_written_logs_error(
    stub_io, monkeypatch, caplog
):
    """run 正常返回但本周 review 版没落（模型一个工具没调）→ 本次 review 失败：
    error 留痕、fail-open 不抛——下一班自动补。"""

    async def lazy_run(self, messages, **kwargs):
        return Message(role=Role.ASSISTANT, content="")  # 不写任何版本

    monkeypatch.setattr(pr_mod.Agent, "run", lazy_run)

    with caplog.at_level("ERROR"):
        await _review()  # 不抛

    assert _sources(stub_io) == ["seed"]
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.asyncio
async def test_run_returns_but_no_version_still_records_cost(stub_io, monkeypatch):
    """没落版的失败班成本照记（token 真烧了），记账与核验解耦。"""

    async def lazy_run(self, messages, **kwargs):
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(pr_mod.Agent, "run", lazy_run)

    await _review()

    assert len(stub_io["costs"]) == 1


@pytest.mark.asyncio
async def test_run_raises_after_version_landed_still_counts(stub_io, monkeypatch):
    """ReAct 循环里 update_persona 落库后模型还会再跑一轮——「review 版已写入但
    后续轮抛错」时 run 抛。核验必须不依赖 run 成败：本周 review 版真落了 = 本班
    成功——成本照记（部分 usage 好过没有）、diff 推送照发、不向上抛。否则次日被
    周幂等挡住（版本已在）→ 成本 / diff 推送永久缺失。"""

    async def failing_after_write_run(self, messages, **kwargs):
        stub_io["events"].append("run")
        stub_io["versions"].append(
            {"source": "review", "written_at": _NOW.isoformat(),
             "narrative": "落了版之后才炸前写下的全文。"}
        )
        raise RuntimeError("post-write round boom")

    monkeypatch.setattr(pr_mod.Agent, "run", failing_after_write_run)

    await _review()  # 不抛

    assert _sources(stub_io) == ["seed", "review"], "版本已在链上"
    assert len(stub_io["costs"]) == 1, "版本落了的班成本必须记（成本永久缺失修复）"
    assert stub_io["pushes"] == [
        {
            "lane": _LANE,
            "persona_id": _PERSONA,
            "old_narrative": "出厂身份正文：她是她。",
            "new_narrative": "落了版之后才炸前写下的全文。",
            "version": 2,
        }
    ], "版本落了的班 diff 推送照发（diff 永久缺失修复）"


@pytest.mark.asyncio
async def test_run_raises_without_version_no_cost_no_push_next_shift_retries(
    stub_io, monkeypatch, caplog
):
    """run 抛且版本没落 = 真失败班：不记成本（照 review.py 同款——run 炸的班
    usage 不完整不入账）、不推送、error 留痕；周幂等仍 False → 下一班可重试补上。"""
    calls = {"n": 0}

    async def flaky_run(self, messages, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom before any write")
        stub_io["versions"].append(
            {"source": "review", "written_at": _NOW.isoformat(),
             "narrative": "次日补班写下的全文。"}
        )
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(pr_mod.Agent, "run", flaky_run)

    with caplog.at_level("ERROR"):
        await _review()  # 首班：炸且没落版，不抛

    assert _sources(stub_io) == ["seed"]
    assert stub_io["costs"] == [], "没落版的炸班不记账（usage 不完整）"
    assert stub_io["pushes"] == []
    assert any(r.levelname == "ERROR" for r in caplog.records)

    await _review(now=_NOW + timedelta(days=1))  # 次日补班（同一周）
    assert _sources(stub_io) == ["seed", "review"], "下一班自动补上"
    assert len(stub_io["pushes"]) == 1


# ---------------------------------------------------------------------------
# 证据窗口：游标增量 / owner 不动游标 / 首跑全量 / 空窗口护栏
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_window_is_all_pages(stub_io, monkeypatch):
    """首跑（链上没有 review 版）：游标 None = 取全部现存日页。"""
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["window_calls"] == [None]


@pytest.mark.asyncio
async def test_incremental_window_uses_last_review_written_at(stub_io, monkeypatch):
    """增量窗口：游标 = 上一条 review 版本的 written_at（上周的版不挡本周班、
    但推走游标）。"""
    stub_io["versions"].append(
        {"source": "review", "written_at": "2026-06-03T11:00:00+08:00",
         "narrative": "上周慢漂的一版。"}
    )
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["window_calls"] == ["2026-06-03T11:00:00+08:00"]


@pytest.mark.asyncio
async def test_owner_version_does_not_move_cursor(stub_io, monkeypatch):
    """owner 盖版不动游标：上周 review + 之后的 owner → 游标仍是 review 那版。"""
    stub_io["versions"].append(
        {"source": "review", "written_at": "2026-06-03T11:00:00+08:00",
         "narrative": "上周慢漂的一版。"}
    )
    stub_io["versions"].append(
        {"source": "owner", "written_at": "2026-06-05T09:00:00+08:00",
         "narrative": "bezhai 盖的版。"}
    )
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["window_calls"] == ["2026-06-03T11:00:00+08:00"]


@pytest.mark.asyncio
async def test_empty_window_skips_model(stub_io, monkeypatch):
    """空窗口护栏：游标之后没有任何新日页 = 这段日子没有新经历 → 不烧模型、
    不落版、不记账（机制安全阀，同空信箱 early-return）。"""
    stub_io["day_pages"] = []
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "messages" not in captured
    assert _sources(stub_io) == ["seed"], "seed 仍先行（v0 落定无害）"
    assert stub_io["costs"] == []


# ---------------------------------------------------------------------------
# 成本：独立 actor、round_id 派生含触发时刻
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_actor_and_round_id_derivation(stub_io, monkeypatch):
    """actor = {persona}:persona_review；round_id 从 (lane, persona, 触发时刻)
    派生——同刻确定不漂移、不同班各自入账。"""
    _mock_run(monkeypatch, stub_io)

    await _review()
    first = stub_io["costs"][0]
    assert first["lane"] == _LANE
    assert first["actor"] == f"{_PERSONA}:persona_review"
    assert first["observed_at"] == _NOW.isoformat()

    # 同 (lane, persona, 触发时刻) → 同 round_id（确定性派生）。
    stub_io["versions"].clear()
    await _review()
    second = stub_io["costs"][1]
    assert second["round_id"] == first["round_id"]

    # 不同触发时刻（次日补班）→ 不同 round_id：两班各自入账。
    stub_io["versions"].clear()
    await _review(now=_NOW + timedelta(days=1))
    third = stub_io["costs"][2]
    assert third["round_id"] != first["round_id"]


# ---------------------------------------------------------------------------
# durable 写失败 ≠ 成功（工具不包 @tool_error，写库失败穿透炸 run）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_durable_write_failure_fails_open(stub_io, monkeypatch, caplog):
    """update_persona 落库失败 → 整次 review 失败：fail-open 不抛、error 留痕、
    不算成功（下一班补）。"""
    import app.life.persona_review_tools as prt
    from app.agent.runtime_context import agent_context
    from app.life.persona_review_tools import update_persona

    async def boom_write(**kwargs):
        raise RuntimeError("pg down during persona version write")

    monkeypatch.setattr(prt, "write_persona_version", boom_write)

    async def dispatching_run(self, messages, *, context=None, **kwargs):
        with agent_context(context):
            await update_persona.invoke({"narrative": "慢漂后的全文。"})
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(pr_mod.Agent, "run", dispatching_run)

    with caplog.at_level("ERROR"):
        await _review()  # fail-open：不向上抛

    assert _sources(stub_io) == ["seed"], "durable 写失败的 review 绝不算成功"
    assert any(r.levelname == "ERROR" for r in caplog.records)


# ---------------------------------------------------------------------------
# update_persona 工具契约：source='review'、时间代码填、缺绑定失败快
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_persona_writes_review_source_with_ambient_keys(monkeypatch):
    """工具体：lane / persona 从 ambient 读、source 钉死 'review'、written_at
    由代码填现实 CST（客观时间不让模型编）。"""
    import app.life.persona_review_tools as prt
    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context
    from app.infra.cst_time import CST
    from app.life.persona_review_tools import update_persona

    written = []

    async def capture_write(**kwargs):
        written.append(kwargs)

    monkeypatch.setattr(prt, "write_persona_version", capture_write)

    ctx = AgentContext(
        persona_id=_PERSONA,
        features={
            FEATURE_PERSONA_REVIEW_LANE: _LANE,
            FEATURE_PERSONA_REVIEW_PERSONA: _PERSONA,
        },
    )
    with agent_context(ctx):
        await update_persona.invoke({"narrative": "慢漂后的全文。"})

    assert len(written) == 1
    w = written[0]
    assert w["lane"] == _LANE
    assert w["persona_id"] == _PERSONA
    assert w["source"] == "review"
    assert w["narrative"] == "慢漂后的全文。"
    parsed = datetime.fromisoformat(w["written_at"])
    assert parsed.utcoffset() == CST.utcoffset(None), "written_at 必须是 CST aware"


@pytest.mark.asyncio
async def test_update_persona_missing_binding_fails_fast():
    """ambient 绑定缺失 → LookupError 穿透（绝不拿空 lane 落出脏 Key）。"""
    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context
    from app.life.persona_review_tools import update_persona

    with agent_context(AgentContext(persona_id=_PERSONA, features={})):
        with pytest.raises(LookupError):
            await update_persona.invoke({"narrative": "全文。"})


@pytest.mark.asyncio
async def test_update_persona_rejects_blank_narrative(monkeypatch):
    """空串 / 纯空白 narrative → ValueError 穿透（工具不包 @tool_error，炸轮走
    fail-open：空产出按失败算、下一班重试——同 sediment 空产出先例），链上零新版本。

    五个读取方注入的是这篇正文：空版一旦落下且本周幂等已满足，不会有自动补救。
    """
    import app.life.persona_review_tools as prt
    from app.agent.context import AgentContext
    from app.agent.runtime_context import agent_context
    from app.life.persona_review_tools import update_persona

    written = []

    async def capture_write(**kwargs):
        written.append(kwargs)

    monkeypatch.setattr(prt, "write_persona_version", capture_write)

    ctx = AgentContext(
        persona_id=_PERSONA,
        features={
            FEATURE_PERSONA_REVIEW_LANE: _LANE,
            FEATURE_PERSONA_REVIEW_PERSONA: _PERSONA,
        },
    )
    with agent_context(ctx):
        for blank in ("", "   ", "\n\t  \n"):
            with pytest.raises(ValueError):
                await update_persona.invoke({"narrative": blank})

    assert written == [], "空白 narrative 绝不落版"


# ---------------------------------------------------------------------------
# single_flight：按 (lane, persona) 包整段；硬超时 < 锁 TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_flight_conflict_yields_silently(stub_io, monkeypatch, fake_redis):
    """锁被持有（另一班正在 review 同一 (lane, persona)）→ 静默让位：不跑、不抛。"""
    captured = _mock_run(monkeypatch, stub_io)
    await fake_redis.set(f"persona_review:{_LANE}:{_PERSONA}", "other-holder")

    await _review()  # 不抛

    assert "messages" not in captured
    assert stub_io["seed_calls"] == []


@pytest.mark.asyncio
async def test_lock_key_scopes_by_persona(stub_io, monkeypatch, fake_redis):
    """锁按 (lane, persona) 分键：别的 persona 的锁不挡这次 review。"""
    captured = _mock_run(monkeypatch, stub_io)
    await fake_redis.set(f"persona_review:{_LANE}:ayana", "other-holder")

    await _review()

    assert "messages" in captured


@pytest.mark.asyncio
async def test_review_hard_timeout_fails_open(stub_io, monkeypatch, caplog):
    """整段 review 挂死（run 不返回）→ 硬超时掐死：error 留痕、不向上抛。"""

    async def hanging_run(self, messages, **kwargs):
        await asyncio.sleep(30)
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(pr_mod.Agent, "run", hanging_run)
    monkeypatch.setattr(pr_mod, "PERSONA_REVIEW_TIMEOUT_SECONDS", 0.05)

    with caplog.at_level("ERROR"):
        await _review()  # 不抛、也绝不真等 30s

    assert _sources(stub_io) == ["seed"], "超时的 review 绝不算成功"
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_review_timeout_below_single_flight_ttl():
    """硬超时必须 < 单飞锁 TTL：锁到期后新班能进，挂死的旧班必须先被掐死。"""
    assert (
        pr_mod.PERSONA_REVIEW_TIMEOUT_SECONDS
        < pr_mod.PERSONA_REVIEW_LOCK_TTL_SECONDS
    )


# ---------------------------------------------------------------------------
# 证据拼装：日页 / 关系页 / 世界阶段，有无两态都如实说、全带时间标注
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evidence_carries_now(stub_io, monkeypatch):
    """输入含现实此刻（她的传记作者知道现在是什么时候）。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "2026-06-10T11:00:00+08:00" in _blob(captured)


@pytest.mark.asyncio
async def test_evidence_includes_day_pages_with_date_and_written_at(stub_io, monkeypatch):
    """日页证据：每页带生活日 + written_at 标注 + 全文。"""
    stub_io["day_pages"] = [
        _day_page(date="2026-06-09", narrative="考完最后一门，腿是软的。",
                  written_at="2026-06-10T05:00:00+08:00"),
    ]
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "考完最后一门" in blob
    assert "2026-06-09" in blob
    assert "2026-06-10T05:00:00+08:00" in blob


@pytest.mark.asyncio
async def test_evidence_includes_relationship_pages(stub_io, monkeypatch):
    """关系页证据：对方 user_id + written_at + 全文。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "他常来群里找我说话" in blob
    assert "user-1" in blob
    assert "2026-06-09T23:40:00+08:00" in blob


@pytest.mark.asyncio
async def test_evidence_marks_npc_relationship_pages(stub_io, monkeypatch):
    """关系页里 ``npc:*`` 的页要在证据里**显式标注是 NPC**（codex 建议 3）。

    persona review 全量读关系页（list_relationship_pages），会读到 NPC 层写下的
    ``npc:名字`` 关系页。喂给模型的证据若把 ``npc:林小满`` 和真人 user_id 一视同仁，
    模型可能把这个 NPC 标识慢慢漂当成真人写进身份正文（身份慢漂误当真人）。所以
    NPC 页的证据要带一个明确「这是 NPC、不是真人用户」的标注。
    """
    stub_io["rel_pages"] = [
        _rel_page(
            other_user_id="npc:林小满",
            narrative="她是绫奈的同桌，总在她慌的时候稳住她。",
        ),
        _rel_page(other_user_id="user-1", narrative="哥哥常来群里找我。"),
    ]
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    # NPC 页内容仍在
    assert "她是绫奈的同桌" in blob
    assert "npc:林小满" in blob
    # 且明确标了「NPC」，让模型知道这不是真人标识
    assert "NPC" in blob, "npc:* 关系页的证据必须显式标注是 NPC（不是真人用户标识）"
    # 真人页不受影响、不被误标 NPC
    assert "哥哥常来群里找我。" in blob


@pytest.mark.asyncio
async def test_evidence_no_relationship_pages_says_so(stub_io, monkeypatch):
    """还没有任何关系页 → 如实说，不冒充。"""
    stub_io["rel_pages"] = []
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "还没有" in _blob(captured)


@pytest.mark.asyncio
async def test_evidence_includes_world_arc(stub_io, monkeypatch):
    """世界阶段证据：最新一版 arc 的 narrative 进 blob。"""
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "一家人刚搬进新的城市" in _blob(captured)


@pytest.mark.asyncio
async def test_evidence_missing_world_arc_says_so(stub_io, monkeypatch):
    """世界阶段空白 → 如实说，不冒充。"""
    stub_io["arc"] = None
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    assert "没有" in _blob(captured)


# ---------------------------------------------------------------------------
# 著作权纪律在 prompt 层：instruction 钉姿态、零剧情事实
# ---------------------------------------------------------------------------


def test_instruction_pins_writing_discipline():
    """instruction 承载工具语义与纪律：整篇重写 / 原样保留 / 出处 / 底色不让渡。"""
    instruction = pr_mod.persona_review_instruction()
    assert "update_persona" in instruction
    assert "原样" in instruction
    assert "重写" in instruction
    assert "底色" in instruction
    assert "证据" in instruction or "出处" in instruction


def test_instruction_has_no_hardcoded_plot_facts():
    """instruction 模板零剧情事实（高考 / 广州 / 角色名 / 日期数字）——宪法。"""
    instruction = pr_mod.persona_review_instruction()
    assert "高考" not in instruction
    assert "广州" not in instruction
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "akao", "ayana"):
        assert name not in instruction
    assert not any(ch.isdigit() for ch in instruction)


# ---------------------------------------------------------------------------
# diff 推送接线（Task 3）：核验成功之后才推；失败路径绝不推；push 异常不回传
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_called_after_verified_review(stub_io, monkeypatch):
    """核验成功 → push 一次：old=本次 review 前链上最新版（首跑 = v0 文本）、
    new=agent 写的新版、version=链上新版号；push 在 run（核验）之后。"""
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["pushes"] == [
        {
            "lane": _LANE,
            "persona_id": _PERSONA,
            "old_narrative": "出厂身份正文：她是她。",
            "new_narrative": "慢漂后的身份正文。",
            "version": 2,
        }
    ]
    assert stub_io["events"] == ["seed", "run", "push"], "push 必须在核验成功之后"


@pytest.mark.asyncio
async def test_no_push_when_run_raises(stub_io, monkeypatch):
    """agent run 炸（review 失败）→ 绝不推。"""

    async def boom_run(self, messages, **kwargs):
        raise RuntimeError("model boom during persona review")

    monkeypatch.setattr(pr_mod.Agent, "run", boom_run)

    await _review()

    assert stub_io["pushes"] == []


@pytest.mark.asyncio
async def test_no_push_when_no_version_written(stub_io, monkeypatch):
    """run 返回但本周没落 review 版（核验失败）→ 绝不推。"""

    async def lazy_run(self, messages, **kwargs):
        return Message(role=Role.ASSISTANT, content="")

    monkeypatch.setattr(pr_mod.Agent, "run", lazy_run)

    await _review()

    assert stub_io["pushes"] == []


@pytest.mark.asyncio
async def test_no_push_when_already_reviewed_this_week(stub_io, monkeypatch):
    """周级幂等早退（本周班已完成）→ 不重推。"""
    stub_io["versions"].append(
        {"source": "review", "written_at": "2026-06-08T11:00:00+08:00",
         "narrative": "本周已慢漂过的一版。"}
    )
    _mock_run(monkeypatch, stub_io)

    await _review()

    assert stub_io["pushes"] == []


@pytest.mark.asyncio
async def test_push_failure_does_not_fail_review(stub_io, monkeypatch, caplog):
    """push 环节炸 → review 仍算成功（版本已落）：不向上抛、error 留痕。"""
    _mock_run(monkeypatch, stub_io)

    async def boom_push(**kwargs):
        raise RuntimeError("push exploded")

    monkeypatch.setattr(pr_mod, "push_persona_diff", boom_push, raising=False)

    with caplog.at_level("ERROR"):
        await _review()  # 不抛

    assert _sources(stub_io) == ["seed", "review"], "review 版照落，不受 push 影响"
    assert any(r.levelname == "ERROR" for r in caplog.records)


@pytest.mark.asyncio
async def test_template_has_no_plot_facts_beyond_inputs(stub_io, monkeypatch):
    """模板静态文案零剧情事实：中性输入下 blob 不出现高考 / 广州 / 角色名。"""
    stub_io["day_pages"] = [
        _day_page(narrative="过了平常的一天。", date="2026-06-09")
    ]
    stub_io["rel_pages"] = []
    stub_io["arc"] = None
    captured = _mock_run(monkeypatch, stub_io)

    await _review()

    blob = _blob(captured)
    assert "高考" not in blob
    assert "广州" not in blob
    for name in ("千凪", "赤尾", "绫奈", "chinagi", "ayana"):
        assert name not in blob
