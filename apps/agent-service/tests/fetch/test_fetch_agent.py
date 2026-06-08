"""抓取 agent 的配置 / 工具集 / 抓取意图 stimulus 契约（刀 3 Task2，纯 agent 主导版）.

抓取回到纯 agent 主导：node 不再先确定性调三个 skill 再把文本喂进来，而是让 agent
拿着三个结构化查询 skill（+ search_web 兜底）自己去查、自己看每个返回的 ``ok``、把
真实数据组织成一段「今天的客观底料」中文话。

所以 ``build_fetch_stimulus`` 只接 ``date``（不再接每源文本），引导 agent 自己调工具、
覆盖天气 / 番剧 / 节假日、对 ``ok=false`` 的源如实说没拿到、绝不编一个顶上。
"""

from __future__ import annotations

import inspect

from app.fetch.agent import (
    FETCH_CFG,
    FETCH_PROMPT_ID,
    FETCH_TOOLS,
    build_fetch_stimulus,
)


def test_stimulus_only_takes_date_not_source_texts():
    """stimulus 回到只接 date —— 不再由 node 预查并把每源文本灌进来（纯 agent 主导）。"""
    params = set(inspect.signature(build_fetch_stimulus).parameters)
    assert params == {"date"}, (
        f"build_fetch_stimulus 应只接 date（agent 自己查），实际 {params}"
    )


def test_stimulus_drives_agent_to_use_tools_and_cover_three_sources():
    """stimulus 引导 agent 自己用工具查、覆盖天气 / 番剧 / 节假日。"""
    s = build_fetch_stimulus(date="2026-06-08")
    assert "2026-06-08" in s
    # 三样都要覆盖到。
    assert "天气" in s
    assert "番" in s
    assert "节假日" in s
    # 引导它去"查"（用工具自己拿数据），而不是被动接收。
    assert "查" in s


def test_stimulus_forbids_fabrication_on_failed_source():
    """stimulus 明确要求：某工具没拿到（ok=false）就如实说没拿到、绝不编一个顶上。"""
    s = build_fetch_stimulus(date="2026-06-08")
    # 必须出现"没拿到 / 别编"这类诚实约束的措辞。
    assert "没拿到" in s
    assert ("不要编" in s) or ("别编" in s) or ("绝不编" in s)


def test_fetch_tools_are_the_three_structured_skills_plus_search():
    """工具集 = 三个结构化查询 skill + search_web 兜底。"""
    names = {t.name for t in FETCH_TOOLS}
    assert {"query_weather", "query_anime_calendar", "query_holiday"} <= names
    assert "search_web" in names


def test_fetch_cfg_uses_offline_model_and_prompt_id():
    """抓取 agent 用 offline-model + langfuse prompt_id=fetch_agent。"""
    assert FETCH_CFG.model_id == "offline-model"
    assert FETCH_CFG.prompt_id == FETCH_PROMPT_ID == "fetch_agent"
