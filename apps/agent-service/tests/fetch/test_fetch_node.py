"""daily_fetch_node —— 通用抓取节点编排契约（刀 3 Task2，纯 agent 主导版）.

每天清晨被 cron 唤醒（经单字段 tick → 翻译补 lane → 本节点）后，抓取**回到纯 agent
主导**：

  1. node 不再先确定性调三个 skill —— 直接跑抓取 agent（offline-model + 三个结构化
     查询 skill + search_web 兜底），由 agent 自己调那几个 skill、看每个返回的 ``ok``、
     把真实数据组织成一段「今天的客观底料」中文话。
  2. 把 agent 组织好的那段话（briefing）落 DailyMaterials（按天幂等）。
  3. 抓取 agent 用 collect_usage 包住 + record_round_cost(actor="fetch") 落成本（刀0）。

这些是节点编排测试：mock Agent.run（不烧真模型 / 不调真 langfuse），验证编排正确性——
不验证 LLM 想得对。最致命的几条：

  * node 自己**不**确定性预调三个 skill（纯 agent 主导：工具只交给 agent）；
  * agent 组织的 briefing 落进 DailyMaterials.briefing；
  * run 传 max_retries=1（关整轮重放）、session_id（按天派生）、model=offline-model、
    工具集含三个 skill + search_web；
  * 成本 record_round_cost(actor="fetch")、带 collect_usage 累计；
  * 落成本失败 best-effort 吞掉、不把一轮真实抓取搞成失败。
"""

from __future__ import annotations

import datetime as _dt

import app.fetch.node as fn
from app.agent.neutral import Message, Role


def saved_rows(monkeypatch):
    """打桩 save_daily_materials，记录落库 kwargs（不碰真库，编排测试）。"""
    rows: list[dict] = []

    async def fake_save(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(fn, "save_daily_materials", fake_save)
    return rows


def cost_calls(monkeypatch):
    """打桩 record_round_cost，记录成本落库调用 kwargs（含 usage）。"""
    calls: list[dict] = []

    async def fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fn, "record_round_cost", fake_record)
    return calls


def _mock_run(monkeypatch, *, briefing="今天广州小雨，是个普通周末。", usage=None):
    """把 Agent.run 换成记录调用参数 + 返回简报文本 + 在 collector 里累加 usage 的桩。"""
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
        captured["cfg"] = self._cfg
        if usage is not None:
            from app.agent.trace import _accumulate_usage

            _accumulate_usage(usage)
        return Message(role=Role.ASSISTANT, content=briefing)

    monkeypatch.setattr(fn.Agent, "run", fake_run)
    return captured


def _fixed_now(monkeypatch, *, y=2026, mo=6, d=8, h=6, mi=0):
    """钉死 now（CST），让 date / session_id / fetched_at 可断言。"""

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, mo, d, h, mi, tzinfo=tz)

    monkeypatch.setattr(fn.cst_time, "datetime", _FixedDateTime)


# ---------------------------------------------------------------------------
# 纯 agent 主导：node 不预调 skill、briefing 落库
# ---------------------------------------------------------------------------


async def test_node_does_not_pre_call_skills(monkeypatch):
    """node 自己**不**确定性预调三个查询 skill —— 工具只交给 agent，由它自己调（纯 agent 主导）。

    把三个 skill 的 invoke 换成会记录调用的桩；node 跑完后这些桩**不该被调到**（agent.run
    被 mock 掉了、不会真的走工具循环，所以唯一可能调到 skill 的就是 node 自己预调那步——
    确认它已删除）。node 不再 import 这三个 skill（纯 agent 主导），从工具模块取它们打桩。
    """
    from app.agent.tools import external_sources as es

    invoked: list[str] = []

    def _spy(tool_obj, name):
        async def fake_invoke(arguments):
            invoked.append(name)
            return {"ok": True}

        monkeypatch.setattr(tool_obj, "invoke", fake_invoke)

    _spy(es.query_weather, "weather")
    _spy(es.query_anime_calendar, "anime")
    _spy(es.query_holiday, "holiday")

    saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    _mock_run(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert invoked == [], "node 不该自己确定性预调任何查询 skill（纯 agent 主导）"


async def test_persists_agent_briefing(monkeypatch):
    """agent 组织好的那段「今天的客观底料」落进 DailyMaterials.briefing。"""
    saved = saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    _mock_run(monkeypatch, briefing="整理好的今天底料话")

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert len(saved) == 1
    row = saved[0]
    assert row["lane"] == "coe-t3"
    assert row["briefing"] == "整理好的今天底料话"
    # 表已简化：不再落每源 *_text / *_ok。
    for gone in (
        "weather_text",
        "anime_text",
        "holiday_text",
        "weather_ok",
        "anime_ok",
        "holiday_ok",
    ):
        assert gone not in row, f"落库不该再带旧字段 {gone}"


async def test_date_and_fetched_at_are_cst(monkeypatch):
    """date 按 CST「今天」算、fetched_at 是 CST aware ISO。"""
    _fixed_now(monkeypatch, h=6)
    saved = saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    _mock_run(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    row = saved[0]
    assert row["date"] == "2026-06-08"
    assert "+08:00" in row["fetched_at"], "fetched_at 必须是 CST aware ISO"


# ---------------------------------------------------------------------------
# agent run 契约：模型 / 工具集 / max_retries / session_id / stimulus
# ---------------------------------------------------------------------------


async def test_run_uses_offline_model(monkeypatch):
    """抓取 agent 用 offline-model（异步后台，和 world/life 一致）。"""
    saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    captured = _mock_run(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert captured["cfg"].model_id == "offline-model"


async def test_run_tools_include_three_skills_and_search_web(monkeypatch):
    """抓取 agent 的工具集 = 三个结构化查询 skill + search_web 兜底（agent 自己调）。"""
    saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    captured = _mock_run(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    tool_names = {t.name for t in captured["tools"]}
    assert {"query_weather", "query_anime_calendar", "query_holiday"} <= tool_names
    assert "search_web" in tool_names, "search_web 必须在工具集里当兜底"


async def test_run_stimulus_only_carries_date(monkeypatch):
    """node 给 agent 的 stimulus 只含「今天是哪天」—— 不再预查、不灌每源文本。"""
    _fixed_now(monkeypatch)
    saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    captured = _mock_run(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert len(captured["messages"]) == 1
    content = captured["messages"][0].content
    assert "2026-06-08" in content
    # 引导 agent 自己去查三样。
    assert "天气" in content and "番" in content and "节假日" in content


async def test_run_passes_max_retries_one(monkeypatch):
    """run 必须 max_retries=1：关掉整轮重放（durable 写不能被重放）。"""
    saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    captured = _mock_run(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert captured["max_retries"] == 1


async def test_run_passes_daily_fetch_session_id(monkeypatch):
    """run 收到按 (lane, "fetch", 今天) 派生的 session_id。"""
    from app.agent.trace import make_session_id

    _fixed_now(monkeypatch)
    saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    captured = _mock_run(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    expected = make_session_id("coe-t3", "fetch", "2026-06-08")
    assert captured["session_id"] == expected


# ---------------------------------------------------------------------------
# 成本观测刀（刀0）：record_round_cost(actor="fetch") + collect_usage 累计
# ---------------------------------------------------------------------------


async def test_records_token_cost_with_fetch_actor(monkeypatch):
    """一轮抓取收口把本轮累计 token 落 PG，actor = "fetch"、带 collect_usage 累计。"""
    _fixed_now(monkeypatch)
    saved_rows(monkeypatch)
    cost_recorded = cost_calls(monkeypatch)
    _mock_run(
        monkeypatch,
        usage={"input": 300, "output": 60, "total": 360, "cache_read_input_tokens": 40},
    )

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert len(cost_recorded) == 1
    rec = cost_recorded[0]
    assert rec["lane"] == "coe-t3"
    assert rec["actor"] == "fetch", "抓取 agent 的 actor 必须是 'fetch'"
    assert rec["usage"]["input"] == 300
    assert rec["usage"]["total"] == 360
    assert rec["usage"]["calls"] == 1
    assert rec["round_id"] == "2026-06-08", "round_id 用当天日期（按天唯一）"
    assert rec["observed_at"]


async def test_cost_record_failure_does_not_fail_round(monkeypatch):
    """落成本失败 best-effort 吞掉，不把一轮真实抓取搞成失败（底料照常落库）。

    打桩真实 record_thinking_tokens 抛错（走 record_round_cost 里真正的 swallow 路径），
    而非打桩 node 的 record_round_cost —— 这样测的是真实吞错语义。
    """
    import app.domain.thinking_cost as tc

    saved = saved_rows(monkeypatch)
    _mock_run(monkeypatch, usage={"input": 1, "output": 1, "total": 2})

    async def boom_record(**kwargs):
        raise RuntimeError("PG down recording cost")

    monkeypatch.setattr(tc, "record_thinking_tokens", boom_record)

    # 不该抛——成本观测是旁路。
    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    # 底料照常落库（成本失败不影响真实抓取收口）。
    assert len(saved) == 1
    assert saved[0]["briefing"]
