"""world 眼睛契约 — 两层感知去看今天、带世界关切的当日叙述（眼睛 Task 3）.

眼睛是 world 的感官器官（认知层归 ``app/world/eyes.py``，旧 ``app/fetch/agent.py``
的独立采编台已消解）。这些测试钉死眼睛的机制层硬约束：

  * 两层感知的 stimulus：**本能扫视** = 开发者定的环境量必看清单（天气、日出日落、
    农历节气、节假日——番剧**不在**必看清单）；**有意张望** = 最新世界阶段 + 最新
    关注，全文带时间标注、缺失如实说空、绝不冒充；
  * 工具箱沿用六件（含番剧工具——保留在手，但 stimulus 只让它在世界阶段 / 关注
    交代到时上手）；
  * 诚实约束：工具 ok=false 如实说没拿到、绝不编一个顶上；
  * 入口编排：读世界阶段 / 关注 → 拼 stimulus → 无会话跑 Agent（max_retries=1、
    session_id 只进 context 做 langfuse 归组标签）→ 返回叙述文本；
  * 眼睛不吞错：失败照实抛给 node（本钟点不落库、下一钟点重试——重试在钟那层）。

看什么、怎么叙述由眼睛推演自主判断（prompt 层约束），代码里没有内容检查器 /
覆盖率校验器（赤尾宪法：不用确定性规则替 agent 决策）。
"""

from __future__ import annotations

import pytest

import app.world.eyes as eyes_mod
from app.agent.neutral import Message, Role
from app.world.arc import WorldArc
from app.world.attention import WorldAttention
from app.world.eyes import (
    INSTINCT_SCAN_ITEMS,
    WORLD_EYES_TOOLS,
    build_eyes_stimulus,
    run_world_eyes,
)


def _arc(**kwargs) -> WorldArc:
    base = {
        "lane": "coe-eyes",
        "narrative": "这一页的世界进展。",
        "turned_at": "2026-06-01T10:00:00+08:00",
    }
    base.update(kwargs)
    return WorldArc(**base)


def _attention(**kwargs) -> WorldAttention:
    base = {
        "lane": "coe-eyes",
        "narrative": "想确认一件还没落定的事。",
        "written_at": "2026-06-09T23:50:00+08:00",
    }
    base.update(kwargs)
    return WorldAttention(**base)


# ---------------------------------------------------------------------------
# 本能扫视：开发者定的环境量必看清单（番剧不在其中）
# ---------------------------------------------------------------------------


def test_instinct_items_are_the_four_env_quantities():
    """本能清单固定为四样环境量（人人被罩着的），番剧不在本能清单里。"""
    assert set(INSTINCT_SCAN_ITEMS) == {"天气", "日出日落", "农历节气", "节假日"}
    assert all("番" not in item for item in INSTINCT_SCAN_ITEMS)


def test_stimulus_contains_date_and_instinct_scan():
    """stimulus 带今天日期 + 本能清单四样，并引导用工具去查（不是被动接收）。"""
    s = build_eyes_stimulus(date="2026-06-10", arc=None, attention=None)
    assert "2026-06-10" in s
    for item in INSTINCT_SCAN_ITEMS:
        assert item in s, f"本能清单的「{item}」必须出现在 stimulus 里"
    assert "查" in s


def test_instinct_section_excludes_anime():
    """本能扫视段里不出现番剧——番剧只归有意张望（世界阶段 / 关注交代到才看）。"""
    s = build_eyes_stimulus(date="2026-06-10", arc=None, attention=None)
    start = s.index("本能扫视")
    end = s.index("有意张望")
    assert "番" not in s[start:end], "番剧不许出现在本能扫视的必看清单段里"


def test_anime_tool_bounded_to_intentional_gaze():
    """stimulus 说清番剧工具的边界：只在世界阶段 / 关注交代到时才用。"""
    s = build_eyes_stimulus(date="2026-06-10", arc=None, attention=None)
    assert "番剧" in s
    assert "只在" in s and "交代" in s


# ---------------------------------------------------------------------------
# 有意张望：世界阶段 / 关注的三态（有 / 无 / 时间标注）
# ---------------------------------------------------------------------------


def test_stimulus_arc_present_carries_fulltext_and_time_label():
    """有世界阶段：全文 + turned_at 时间标注进 stimulus（眼睛要知道这页是多久前翻的）。"""
    s = build_eyes_stimulus(
        date="2026-06-10",
        arc=_arc(narrative="世界阶段全文在此。", turned_at="2026-06-01T10:00:00+08:00"),
        attention=None,
    )
    assert "世界阶段全文在此。" in s
    assert "2026-06-01T10:00:00+08:00" in s


def test_stimulus_arc_missing_is_honest():
    """没有世界阶段：如实说世界还没写下走到哪，绝不冒充。"""
    s = build_eyes_stimulus(date="2026-06-10", arc=None, attention=None)
    assert "世界还没写下走到哪" in s


def test_stimulus_attention_present_carries_fulltext_and_time_label():
    """有关注：全文 + written_at 时间标注进 stimulus，并要求如实回应看到 / 没看到。"""
    s = build_eyes_stimulus(
        date="2026-06-10",
        arc=None,
        attention=_attention(
            narrative="关注全文在此。", written_at="2026-06-09T23:50:00+08:00"
        ),
    )
    assert "关注全文在此。" in s
    assert "2026-06-09T23:50:00+08:00" in s
    assert "如实" in s


def test_stimulus_attention_missing_is_honest():
    """没有关注：如实说还没有人交代过眼睛要看什么，眼睛只做本能扫视。"""
    s = build_eyes_stimulus(date="2026-06-10", arc=None, attention=None)
    assert "还没有人交代过眼睛要看什么" in s


def test_stimulus_forbids_fabrication_on_failed_source():
    """诚实约束：工具 ok=false 就如实说没拿到、绝不编一个顶上（兜底语义不变）。"""
    s = build_eyes_stimulus(date="2026-06-10", arc=None, attention=None)
    assert "没拿到" in s
    assert "绝不编" in s
    assert "search_web" in s


# ---------------------------------------------------------------------------
# 工具箱：沿用六件（番剧保留在手、边界在 stimulus）
# ---------------------------------------------------------------------------


def test_eyes_tools_are_the_six_skills():
    """工具箱 = 五个结构化查询 skill + search_web 兜底（番剧工具保留）。"""
    names = {t.name for t in WORLD_EYES_TOOLS}
    assert names == {
        "query_weather",
        "query_anime_calendar",
        "query_holiday",
        "query_sun_times",
        "query_lunar_term",
        "search_web",
    }


# ---------------------------------------------------------------------------
# 入口编排：读两样 → 拼 stimulus → 无会话跑 Agent → 返回叙述
# ---------------------------------------------------------------------------


def _stub_reads(monkeypatch, *, arc=None, attention=None):
    """打桩眼睛的两个读函数（不碰真库，编排测试）。"""

    async def fake_read_arc(*, lane):
        return arc

    async def fake_read_attention(*, lane):
        return attention

    monkeypatch.setattr(eyes_mod, "read_world_arc", fake_read_arc)
    monkeypatch.setattr(eyes_mod, "read_world_attention", fake_read_attention)


def _mock_run(monkeypatch, *, briefing="今天的当日叙述。", raises=None):
    """把 Agent.run 换成记录调用参数 + 返回叙述文本（或抛错）的桩。"""
    captured: dict = {}

    async def fake_run(
        self, messages, *, prompt_vars=None, context=None, session_id=None,
        max_retries=2,
    ):
        captured["messages"] = messages
        captured["context"] = context
        captured["session_id"] = session_id
        captured["max_retries"] = max_retries
        captured["tools"] = self._tools
        captured["cfg"] = self._cfg
        if raises is not None:
            raise raises
        return Message(role=Role.ASSISTANT, content=briefing)

    monkeypatch.setattr(eyes_mod.Agent, "run", fake_run)
    return captured


async def test_run_reads_arc_and_attention_into_single_user_message(monkeypatch):
    """入口读最新世界阶段 + 最新关注，两层感知拼成单条 user 消息喂给 Agent。"""
    _stub_reads(
        monkeypatch,
        arc=_arc(narrative="世界走到这一页。"),
        attention=_attention(narrative="今天专门看这件事。"),
    )
    captured = _mock_run(monkeypatch)

    await run_world_eyes(lane="coe-eyes", date="2026-06-10")

    assert len(captured["messages"]) == 1
    msg = captured["messages"][0]
    assert msg.role == Role.USER
    assert "2026-06-10" in msg.content
    assert "世界走到这一页。" in msg.content
    assert "今天专门看这件事。" in msg.content


async def test_run_is_sessionless_with_trace_label_only(monkeypatch):
    """无会话（同反思）：run 不传 session_id；session_id 只进 context 做 langfuse 归组。"""
    from app.agent.trace import make_session_id

    _stub_reads(monkeypatch)
    captured = _mock_run(monkeypatch)

    await run_world_eyes(lane="coe-eyes", date="2026-06-10")

    assert captured["session_id"] is None, "眼睛无会话：每钟点从证据现看，不续接 transcript"
    assert captured["context"].session_id == make_session_id(
        "coe-eyes", "world_eyes", "2026-06-10"
    )


async def test_run_passes_max_retries_one(monkeypatch):
    """max_retries=1：重试只留钟那一层（下一钟点），不在 Agent 里整轮重放烧 token。"""
    _stub_reads(monkeypatch)
    captured = _mock_run(monkeypatch)

    await run_world_eyes(lane="coe-eyes", date="2026-06-10")

    assert captured["max_retries"] == 1


async def test_run_uses_world_eyes_cfg(monkeypatch):
    """AgentConfig：prompt id world_eyes、offline-model、trace name world-eyes。"""
    _stub_reads(monkeypatch)
    captured = _mock_run(monkeypatch)

    await run_world_eyes(lane="coe-eyes", date="2026-06-10")

    cfg = captured["cfg"]
    assert cfg.prompt_id == "world_eyes"
    assert cfg.model_id == "offline-model"
    assert cfg.trace_name == "world-eyes"


async def test_run_hands_agent_the_six_tools(monkeypatch):
    """Agent 拿到的工具箱就是 WORLD_EYES_TOOLS 六件。"""
    _stub_reads(monkeypatch)
    captured = _mock_run(monkeypatch)

    await run_world_eyes(lane="coe-eyes", date="2026-06-10")

    assert {t.name for t in captured["tools"]} == {t.name for t in WORLD_EYES_TOOLS}


async def test_run_returns_briefing_text(monkeypatch):
    """入口返回 Agent 组织好的叙述文本（落库是 node 的事，眼睛不落库）。"""
    _stub_reads(monkeypatch)
    _mock_run(monkeypatch, briefing="带世界关切的当日叙述。")

    out = await run_world_eyes(lane="coe-eyes", date="2026-06-10")

    assert out == "带世界关切的当日叙述。"


async def test_run_failure_propagates(monkeypatch):
    """眼睛不吞错：Agent 失败照实抛——node 本钟点不落库、下一钟点 cron 重试。"""
    _stub_reads(monkeypatch)
    _mock_run(monkeypatch, raises=RuntimeError("model down"))

    with pytest.raises(RuntimeError, match="model down"):
        await run_world_eyes(lane="coe-eyes", date="2026-06-10")
