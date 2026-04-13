# Life Engine Agent Team Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three-tier schedule generation pipeline (monthly → weekly → daily) with an Agent Team architecture that produces diverse, high-quality daily schedules through parallel wild agents, sister theater, and persona-filtered curation.

**Architecture:** 4 Wild Agents (persona-blind) generate parallel stimuli → real search provides factual anchors → Sister Theater generates shared family events → Curator (persona_lite) filters → Writer (persona_core) composes → Critic reviews. Shared steps run once; per-persona steps run for each persona.

**Tech Stack:** Python 3.12, LangChain/LangGraph, Langfuse prompts, arq cron, asyncio

**Spec:** `docs/superpowers/specs/2026-04-13-life-engine-agent-team-design.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `apps/agent-service/app/life/_date_utils.py` | Shared CST timezone, WEEKDAY_CN, get_season() |
| `apps/agent-service/app/life/wild_agents.py` | 4 wild agent configs + parallel runner |
| `apps/agent-service/app/life/sister_theater.py` | Sister theater generation |
| `apps/agent-service/tests/unit/life/test_wild_agents.py` | Wild agent unit tests |
| `apps/agent-service/tests/unit/life/test_sister_theater.py` | Sister theater unit tests |
| `apps/agent-service/tests/unit/life/test_schedule.py` | Rewritten schedule pipeline tests |

### Modified Files

| File | Changes |
|------|---------|
| `apps/agent-service/app/life/schedule.py` | Delete monthly/weekly (L157-308), rewrite daily as agent team |
| `apps/agent-service/app/workers/cron.py` | Remove `cron_generate_monthly_plan`, `cron_generate_weekly_plan`, update daily |
| `apps/agent-service/app/workers/arq_settings.py` | Remove monthly/weekly imports (L20,23) and cron entries (L118-122) |
| `apps/agent-service/app/api/routes.py` | Remove monthly/weekly from trigger-schedule (L129-132, L138-139) |

### Langfuse Prompts to Create

| Prompt Name | Type | Variables |
|-------------|------|-----------|
| `wild_agent_internet` | text | date, weekday, season |
| `wild_agent_city` | text | date, season, weather |
| `wild_agent_rabbithole` | text | _(none)_ |
| `wild_agent_mood` | text | date, season |
| `sister_theater` | text | date, weekday, season, prev_theater_summary |
| `daily_curator` | text | persona_lite, all_materials |

### Langfuse Prompts to Update

| Prompt Name | Removed Variables | Added Variables |
|-------------|------------------|-----------------|
| `schedule_daily_writer` | weekly_plan, ideation_output | curated_materials, theater |

### Langfuse Prompts No Longer Referenced (can archive later)

`schedule_daily_ideation`, `schedule_monthly`, `schedule_weekly`

---

## Task 1: Shared date utilities

**Files:**
- Create: `apps/agent-service/app/life/_date_utils.py`

- [ ] **Step 1: Create `_date_utils.py`**

```python
"""Shared date/time constants for the life engine modules."""

from datetime import timedelta, timezone

CST = timezone(timedelta(hours=8))

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

_SEASON_MAP = {
    (3, 4, 5): "春天",
    (6, 7, 8): "夏天",
    (9, 10, 11): "秋天",
    (12, 1, 2): "冬天",
}


def get_season(month: int) -> str:
    for months, name in _SEASON_MAP.items():
        if month in months:
            return name
    return "未知"
```

- [ ] **Step 2: Commit**

```bash
cd apps/agent-service
git add app/life/_date_utils.py
git commit -m "refactor(life): extract shared date utils to _date_utils.py"
```

---

## Task 2: Wild agents module + tests

**Files:**
- Create: `apps/agent-service/app/life/wild_agents.py`
- Test: `apps/agent-service/tests/unit/life/test_wild_agents.py`

- [ ] **Step 1: Write tests**

```python
"""Tests for app.life.wild_agents — parallel wild agent execution."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.wild_agents import run_wild_agents

MODULE = "app.life.wild_agents"


def _mock_agent_factory(*, fail_prompt_id: str | None = None):
    """Return a side_effect for Agent() that builds per-config mocks."""

    def _make(cfg, **kwargs):
        instance = AsyncMock()
        if fail_prompt_id and cfg.prompt_id == fail_prompt_id:
            instance.run.side_effect = RuntimeError("agent failed")
        else:
            instance.run.return_value = MagicMock(
                content=f"output from {cfg.prompt_id}"
            )
        return instance

    return _make


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_all_agents_succeed(MockAgent):
    MockAgent.side_effect = _mock_agent_factory()

    result = await run_wild_agents(date(2026, 4, 15))

    assert "互联网漫游" in result
    assert "城市观察" in result
    assert "兔子洞" in result
    assert "情绪天气" in result
    assert MockAgent.call_count == 4


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_one_agent_fails_others_continue(MockAgent):
    MockAgent.side_effect = _mock_agent_factory(fail_prompt_id="wild_agent_city")

    result = await run_wild_agents(date(2026, 4, 15))

    assert "互联网漫游" in result
    assert "城市观察" not in result
    assert "兔子洞" in result
    assert "情绪天气" in result


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_passes_correct_date_vars(MockAgent):
    instances = {}

    def _make(cfg, **kwargs):
        inst = AsyncMock()
        inst.run.return_value = MagicMock(content="ok")
        instances[cfg.prompt_id] = inst
        return inst

    MockAgent.side_effect = _make

    await run_wild_agents(date(2026, 4, 15), weather="多云 22°C")

    # Internet agent gets date/weekday/season
    call_kwargs = instances["wild_agent_internet"].run.call_args
    pvars = call_kwargs.kwargs.get("prompt_vars") or call_kwargs[1].get("prompt_vars")
    assert pvars["date"] == "2026-04-15"
    assert pvars["weekday"] == "周二"
    assert pvars["season"] == "春天"

    # City agent gets weather
    call_kwargs = instances["wild_agent_city"].run.call_args
    pvars = call_kwargs.kwargs.get("prompt_vars") or call_kwargs[1].get("prompt_vars")
    assert pvars["weather"] == "多云 22°C"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/agent-service && uv run pytest tests/unit/life/test_wild_agents.py -v
```

Expected: ImportError — `app.life.wild_agents` does not exist yet.

- [ ] **Step 3: Implement `wild_agents.py`**

```python
"""Wild Agents — four persona-blind agents that generate diverse stimuli.

Each agent imagines "what floated past an 18-year-old girl today" from a
different angle. They do NOT know persona identity, interests, or location.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig, extract_text
from app.life._date_utils import WEEKDAY_CN, get_season

logger = logging.getLogger(__name__)

_WILD_INTERNET_CFG = AgentConfig("wild_agent_internet", "offline-model", "wild-internet")
_WILD_CITY_CFG = AgentConfig("wild_agent_city", "offline-model", "wild-city")
_WILD_RABBITHOLE_CFG = AgentConfig("wild_agent_rabbithole", "offline-model", "wild-rabbithole")
_WILD_MOOD_CFG = AgentConfig("wild_agent_mood", "offline-model", "wild-mood")

_LABELS = ["互联网漫游", "城市观察", "兔子洞", "情绪天气"]


async def _run_one(cfg: AgentConfig, prompt_vars: dict) -> str:
    result = await Agent(cfg).run(
        messages=[HumanMessage(content="开始。")],
        prompt_vars=prompt_vars,
    )
    return extract_text(result.content)


async def run_wild_agents(target_date: date, weather: str = "") -> str:
    """Run 4 wild agents in parallel. Returns combined materials text.

    Wild agents don't know persona — only "18岁中国女生" as the base profile.
    """
    season = get_season(target_date.month)
    weekday = WEEKDAY_CN[target_date.weekday()]
    date_str = target_date.isoformat()

    tasks = [
        _run_one(_WILD_INTERNET_CFG, {"date": date_str, "weekday": weekday, "season": season}),
        _run_one(_WILD_CITY_CFG, {"date": date_str, "season": season, "weather": weather}),
        _run_one(_WILD_RABBITHOLE_CFG, {}),
        _run_one(_WILD_MOOD_CFG, {"date": date_str, "season": season}),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    sections = []
    for label, result in zip(_LABELS, results):
        if isinstance(result, Exception):
            logger.warning("Wild agent '%s' failed: %s", label, result)
            continue
        if result:
            sections.append(f"--- {label} ---\n{result}")

    return "\n\n".join(sections)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd apps/agent-service && uv run pytest tests/unit/life/test_wild_agents.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd apps/agent-service
git add app/life/wild_agents.py tests/unit/life/test_wild_agents.py
git commit -m "feat(life): add wild agents module — 4 parallel persona-blind stimuli generators"
```

---

## Task 3: Sister theater module + tests

**Files:**
- Create: `apps/agent-service/app/life/sister_theater.py`
- Test: `apps/agent-service/tests/unit/life/test_sister_theater.py`

- [ ] **Step 1: Write tests**

```python
"""Tests for app.life.sister_theater — family event generation."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.sister_theater import run_sister_theater

MODULE = "app.life.sister_theater"


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_generates_theater(MockAgent):
    mock_instance = AsyncMock()
    mock_instance.run.return_value = MagicMock(
        content="[上午] 绫奈书包拉链坏了\n[下午] 千凪带了公司的蛋糕回来"
    )
    MockAgent.return_value = mock_instance

    result = await run_sister_theater(date(2026, 4, 15))

    assert "绫奈" in result or "千凪" in result
    mock_instance.run.assert_called_once()


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_passes_prev_summary(MockAgent):
    mock_instance = AsyncMock()
    mock_instance.run.return_value = MagicMock(content="theater output")
    MockAgent.return_value = mock_instance

    await run_sister_theater(date(2026, 4, 15), prev_theater_summary="昨天千凪加班")

    call_kwargs = mock_instance.run.call_args
    pvars = call_kwargs.kwargs.get("prompt_vars") or call_kwargs[1].get("prompt_vars")
    assert pvars["prev_theater_summary"] == "昨天千凪加班"


@pytest.mark.asyncio
@patch(f"{MODULE}.Agent")
async def test_default_prev_summary_when_empty(MockAgent):
    mock_instance = AsyncMock()
    mock_instance.run.return_value = MagicMock(content="theater output")
    MockAgent.return_value = mock_instance

    await run_sister_theater(date(2026, 4, 15))

    call_kwargs = mock_instance.run.call_args
    pvars = call_kwargs.kwargs.get("prompt_vars") or call_kwargs[1].get("prompt_vars")
    assert pvars["prev_theater_summary"] == "（昨天没有小剧场记录）"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/agent-service && uv run pytest tests/unit/life/test_sister_theater.py -v
```

Expected: ImportError.

- [ ] **Step 3: Implement `sister_theater.py`**

```python
"""Sister Theater — shared family events for the three sisters.

Generates daily household happenings involving 赤尾, 千凪, 绫奈, and 原智鸿.
Only personality outlines are provided — no interest details — to avoid
biasing the theater toward any persona's hobbies.
"""

from __future__ import annotations

import logging
from datetime import date

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig, extract_text
from app.life._date_utils import WEEKDAY_CN, get_season

logger = logging.getLogger(__name__)

_THEATER_CFG = AgentConfig("sister_theater", "offline-model", "sister-theater")


async def run_sister_theater(
    target_date: date,
    prev_theater_summary: str = "",
) -> str:
    """Generate 5-6 daily family events for the three sisters.

    All personas share the same theater output — each persona's Writer
    picks the events she cares about from her own perspective.
    """
    result = await Agent(_THEATER_CFG).run(
        messages=[HumanMessage(content="生成今天的家庭琐事。")],
        prompt_vars={
            "date": target_date.isoformat(),
            "weekday": WEEKDAY_CN[target_date.weekday()],
            "season": get_season(target_date.month),
            "prev_theater_summary": prev_theater_summary or "（昨天没有小剧场记录）",
        },
    )
    return extract_text(result.content)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd apps/agent-service && uv run pytest tests/unit/life/test_sister_theater.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd apps/agent-service
git add app/life/sister_theater.py tests/unit/life/test_sister_theater.py
git commit -m "feat(life): add sister theater module — shared family event generation"
```

---

## Task 4: Rewrite schedule.py + tests

This is the core task. Delete monthly/weekly plan generation. Rewrite daily plan as the Agent Team pipeline.

**Files:**
- Rewrite: `apps/agent-service/app/life/schedule.py`
- Test: `apps/agent-service/tests/unit/life/test_schedule.py`

- [ ] **Step 1: Write tests for the new pipeline**

```python
"""Tests for app.life.schedule — Agent Team daily plan pipeline."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.life.schedule import (
    _fetch_search_anchors,
    _format_recent_schedules,
    _run_shared_pipeline,
    generate_daily_plan,
)

MODULE = "app.life.schedule"


# ---------------------------------------------------------------------------
# _format_recent_schedules
# ---------------------------------------------------------------------------


def test_format_recent_schedules_empty():
    assert _format_recent_schedules([]) == "（没有前几天的日程）"


def test_format_recent_schedules_formats_correctly():
    s1 = MagicMock(period_start="2026-04-14", content="昨天的日程")
    s2 = MagicMock(period_start="2026-04-13", content="前天的日程")
    result = _format_recent_schedules([s1, s2])
    assert "[2026-04-14]" in result
    assert "昨天的日程" in result
    assert "---" in result


# ---------------------------------------------------------------------------
# _fetch_search_anchors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(f"{MODULE}.search_web")
async def test_search_anchors_returns_results(mock_search):
    mock_search.ainvoke = AsyncMock(return_value="[1] 杭州今天 22°C 多云")

    result = await _fetch_search_anchors(date(2026, 4, 15))

    assert "杭州" in result
    assert mock_search.ainvoke.call_count == 3  # 3 queries


@pytest.mark.asyncio
@patch(f"{MODULE}.search_web")
async def test_search_anchors_handles_failure(mock_search):
    mock_search.ainvoke = AsyncMock(side_effect=RuntimeError("timeout"))

    result = await _fetch_search_anchors(date(2026, 4, 15))

    assert result == "（搜索锚点获取失败）"


# ---------------------------------------------------------------------------
# _run_shared_pipeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(f"{MODULE}.run_sister_theater", new_callable=AsyncMock)
@patch(f"{MODULE}._fetch_search_anchors", new_callable=AsyncMock)
@patch(f"{MODULE}.run_wild_agents", new_callable=AsyncMock)
async def test_shared_pipeline_runs_all_three(mock_wild, mock_search, mock_theater):
    mock_wild.return_value = "wild materials"
    mock_search.return_value = "search anchors"
    mock_theater.return_value = "theater events"

    wild, anchors, theater = await _run_shared_pipeline(date(2026, 4, 15))

    assert wild == "wild materials"
    assert anchors == "search anchors"
    assert theater == "theater events"


@pytest.mark.asyncio
@patch(f"{MODULE}.run_sister_theater", new_callable=AsyncMock)
@patch(f"{MODULE}._fetch_search_anchors", new_callable=AsyncMock)
@patch(f"{MODULE}.run_wild_agents", new_callable=AsyncMock)
async def test_shared_pipeline_handles_partial_failure(mock_wild, mock_search, mock_theater):
    mock_wild.side_effect = RuntimeError("wild failed")
    mock_search.return_value = "search anchors"
    mock_theater.return_value = "theater events"

    wild, anchors, theater = await _run_shared_pipeline(date(2026, 4, 15))

    assert wild == ""  # failed → empty string
    assert anchors == "search anchors"
    assert theater == "theater events"


# ---------------------------------------------------------------------------
# generate_daily_plan (integration-level mock test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@patch(f"{MODULE}._run_persona_pipeline", new_callable=AsyncMock)
@patch(f"{MODULE}._run_shared_pipeline", new_callable=AsyncMock)
async def test_generate_daily_plan_wires_shared_to_persona(mock_shared, mock_persona):
    mock_shared.return_value = ("wild", "search", "theater")
    mock_persona.return_value = "schedule content"

    result = await generate_daily_plan("akao", date(2026, 4, 15))

    assert result == "schedule content"
    mock_persona.assert_called_once_with("akao", date(2026, 4, 15), "wild", "search", "theater")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/agent-service && uv run pytest tests/unit/life/test_schedule.py -v
```

Expected: ImportError or AttributeError — new functions don't exist yet.

- [ ] **Step 3: Rewrite `schedule.py`**

Replace the entire file content with:

```python
"""Schedule — Agent Team daily plan generation.

Pipeline: Wild Agents (parallel) + Search Anchors + Sister Theater
        → Curator (persona filter) → Writer → Critic

Monthly and weekly plans have been removed. Daily plans are generated
directly from diverse external stimuli instead of narrowing funnels.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

from langchain_core.messages import HumanMessage

from app.agent.core import Agent, AgentConfig, extract_text
from app.agent.tools.search import search_web
from app.data import queries as Q
from app.data.models import AkaoSchedule
from app.data.session import get_session
from app.infra.config import settings
from app.life._date_utils import CST, WEEKDAY_CN, get_season
from app.life.sister_theater import run_sister_theater
from app.life.wild_agents import run_wild_agents
from app.memory._persona import load_persona

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Agent configs
# ---------------------------------------------------------------------------

_CURATOR_CFG = AgentConfig("daily_curator", "offline-model", "daily-curator")
_WRITER_CFG = AgentConfig("schedule_daily_writer", "offline-model", "schedule-writer")
_CRITIC_CFG = AgentConfig("schedule_daily_critic", "offline-model", "schedule-critic")


def _schedule_model() -> str:
    """Model used for schedule generation (shares diary_model config)."""
    return settings.diary_model


# ---------------------------------------------------------------------------
# Search anchors (factual reality anchoring)
# ---------------------------------------------------------------------------


async def _fetch_search_anchors(target_date: date) -> str:
    """Fetch 3-5 factual search results to anchor the schedule in reality.

    Queries are system-constructed (not LLM-generated).
    """
    date_str = target_date.isoformat()
    month = target_date.month
    queries = [
        f"杭州 {date_str} 天气",
        f"{target_date.year}年{month}月新番 本周更新",
        "杭州 老城区 最近 新开 关门 展览",
    ]

    results = []
    for q in queries:
        try:
            text = await search_web.ainvoke({"query": q, "num": 2})
            if text and text != "未搜索到相关结果":
                results.append(f"[{q}]\n{text[:500]}")
        except Exception as e:
            logger.warning("Search anchor '%s' failed: %s", q, e)

    return "\n\n".join(results) if results else "（搜索锚点获取失败）"


# ---------------------------------------------------------------------------
# Shared pipeline (persona-independent, run once per day)
# ---------------------------------------------------------------------------


async def _run_shared_pipeline(target_date: date) -> tuple[str, str, str]:
    """Run shared steps in parallel: wild agents + search anchors + sister theater.

    Returns (wild_materials, search_anchors, theater_text).
    """
    wild_task = run_wild_agents(target_date)
    search_task = _fetch_search_anchors(target_date)
    theater_task = run_sister_theater(target_date)

    results = await asyncio.gather(wild_task, search_task, theater_task, return_exceptions=True)

    wild = results[0] if not isinstance(results[0], Exception) else ""
    anchors = results[1] if not isinstance(results[1], Exception) else ""
    theater = results[2] if not isinstance(results[2], Exception) else ""

    for i, label in enumerate(["Wild agents", "Search anchors", "Sister theater"]):
        if isinstance(results[i], Exception):
            logger.warning("%s failed: %s", label, results[i])

    return wild, anchors, theater


# ---------------------------------------------------------------------------
# Per-persona agent helpers
# ---------------------------------------------------------------------------


async def _run_curator(persona_lite: str, all_materials: str) -> str:
    """Curator Agent: filter materials through persona's perspective."""
    result = await Agent(_CURATOR_CFG).run(
        messages=[HumanMessage(content="从素材池里筛选今天会在意的东西。")],
        prompt_vars={"persona_lite": persona_lite, "all_materials": all_materials},
    )
    return extract_text(result.content)


async def _run_writer(
    persona_core: str,
    curated_materials: str,
    theater: str,
    yesterday_journal: str,
    target_date: date,
    previous_output: str = "",
    critic_feedback: str = "",
) -> str:
    """Writer Agent: compose the daily journal/schedule."""
    result = await Agent(_WRITER_CFG).run(
        messages=[HumanMessage(content="写今天的手帐")],
        prompt_vars={
            "persona_core": persona_core,
            "date": target_date.isoformat(),
            "weekday": WEEKDAY_CN[target_date.weekday()],
            "is_weekend": "周末！" if target_date.weekday() >= 5 else "",
            "yesterday_journal": yesterday_journal,
            "curated_materials": curated_materials,
            "theater": theater,
            "previous_output": previous_output,
            "critic_feedback": critic_feedback,
        },
    )
    return extract_text(result.content)


async def _run_critic(
    schedule_text: str,
    recent_schedules_text: str,
    persona_name: str = "",
) -> str:
    """Critic Agent: review quality, return PASS or revision notes."""
    result = await Agent(_CRITIC_CFG).run(
        messages=[HumanMessage(content="审查今天的手帐质量")],
        prompt_vars={
            "persona_name": persona_name,
            "today_schedule": schedule_text,
            "recent_schedules": recent_schedules_text,
        },
    )
    return extract_text(result.content)


# ---------------------------------------------------------------------------
# Recent schedules helper
# ---------------------------------------------------------------------------


async def _get_recent_daily_schedules(
    before_date: date, persona_id: str, count: int = 3
) -> list[AkaoSchedule]:
    """Fetch recent daily schedules before a date (for Critic context)."""
    async with get_session() as s:
        results = await Q.list_schedules(
            s, plan_type="daily", persona_id=persona_id,
            active_only=True, limit=count + 5,
        )
    return [sched for sched in results if sched.period_start < before_date.isoformat()][:count]


def _format_recent_schedules(schedules: list[AkaoSchedule]) -> str:
    if not schedules:
        return "（没有前几天的日程）"
    return "\n\n---\n\n".join(
        f"[{sched.period_start}]\n{sched.content}" for sched in schedules
    )


# ---------------------------------------------------------------------------
# Per-persona pipeline
# ---------------------------------------------------------------------------


async def _run_persona_pipeline(
    persona_id: str,
    target_date: date,
    wild_materials: str,
    search_anchors: str,
    theater: str,
) -> str | None:
    """Per-persona pipeline: curator → writer → critic loop.

    Returns the final schedule text, or None on failure.
    """
    date_str = target_date.isoformat()

    # Skip if already generated
    async with get_session() as s:
        existing = await Q.find_plan_for_period(s, "daily", date_str, date_str, persona_id)
    if existing:
        logger.info("[%s] Daily plan already exists for %s, skip", persona_id, date_str)
        return existing.content

    pc = await load_persona(persona_id)

    # Combine materials for curator input
    all_materials = wild_materials
    if search_anchors:
        all_materials += f"\n\n--- 真实搜索锚点 ---\n{search_anchors}"

    # Yesterday's journal
    async with get_session() as s:
        recent_dailies = await Q.find_recent_fragments_by_grain(
            s, persona_id, "daily", limit=1
        )
    yesterday_journal = recent_dailies[0].content if recent_dailies else "（昨天没有写日志）"

    # Recent schedules for critic
    recent = await _get_recent_daily_schedules(target_date, persona_id)
    recent_schedules_text = _format_recent_schedules(recent)

    # Curator: filter materials through persona lens
    try:
        curated = await _run_curator(pc.persona_lite, all_materials)
    except Exception as e:
        logger.warning("[%s] Curator failed, using raw materials: %s", persona_id, e)
        curated = all_materials[:2000]

    # Writer → Critic loop (max 3 attempts)
    feedback = ""
    previous_output = ""
    schedule_text = ""

    for attempt in range(3):
        schedule_text = await _run_writer(
            persona_core=pc.persona_core,
            curated_materials=curated,
            theater=theater,
            yesterday_journal=yesterday_journal,
            target_date=target_date,
            previous_output=previous_output,
            critic_feedback=feedback,
        )

        critic_result = await _run_critic(
            schedule_text=schedule_text,
            recent_schedules_text=recent_schedules_text,
            persona_name=pc.display_name,
        )

        if critic_result.strip().upper().startswith("PASS"):
            logger.info("[%s] Daily plan passed critic on attempt %d", persona_id, attempt + 1)
            break

        logger.info(
            "[%s] Critic rejected (attempt %d): %s",
            persona_id, attempt + 1, critic_result[:100],
        )
        previous_output = schedule_text
        feedback = critic_result

    if not schedule_text:
        logger.warning("[%s] Pipeline produced empty daily plan for %s", persona_id, date_str)
        return None

    # Persist
    async with get_session() as s:
        await Q.upsert_schedule(
            s,
            AkaoSchedule(
                plan_type="daily",
                period_start=date_str,
                period_end=date_str,
                persona_id=persona_id,
                content=schedule_text,
                model=_schedule_model(),
            ),
        )

    logger.info("[%s] Daily plan generated for %s: %d chars", persona_id, date_str, len(schedule_text))
    return schedule_text


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def generate_daily_plan(
    persona_id: str, target_date: date | None = None
) -> str | None:
    """Generate a daily plan for a single persona (admin trigger).

    Runs the full pipeline including shared steps.
    """
    if target_date is None:
        target_date = datetime.now(CST).date()

    wild, anchors, theater = await _run_shared_pipeline(target_date)
    return await _run_persona_pipeline(persona_id, target_date, wild, anchors, theater)


async def generate_all_daily_plans(target_date: date | None = None) -> None:
    """Generate daily plans for all personas (cron job).

    Shared steps (wild agents + search + theater) run once.
    Per-persona steps (curator + writer + critic) run for each persona.
    """
    if target_date is None:
        target_date = datetime.now(CST).date()

    logger.info("Generating daily plans for all personas: %s", target_date.isoformat())

    wild, anchors, theater = await _run_shared_pipeline(target_date)

    async with get_session() as s:
        persona_ids = await Q.list_all_persona_ids(s)

    for persona_id in persona_ids:
        try:
            await _run_persona_pipeline(persona_id, target_date, wild, anchors, theater)
        except Exception:
            logger.exception("[%s] daily plan generation failed", persona_id)


# ---------------------------------------------------------------------------
# Schedule context builder (for injecting into chat system prompt)
# ---------------------------------------------------------------------------


async def build_schedule_context(persona_id: str) -> str:
    """Build the current daily schedule context for system prompt injection.

    Returns empty string if no daily plan exists for today.
    """
    now = datetime.now(CST)
    today = now.strftime("%Y-%m-%d")

    async with get_session() as s:
        daily = await Q.find_plan_for_period(s, "daily", today, today, persona_id)

    if not daily:
        return ""
    return daily.content
```

- [ ] **Step 4: Run new tests**

```bash
cd apps/agent-service && uv run pytest tests/unit/life/test_schedule.py -v
```

Expected: All PASS.

- [ ] **Step 5: Run existing life tests to check for regressions**

```bash
cd apps/agent-service && uv run pytest tests/unit/life/ -v
```

Expected: All PASS (test_engine.py imports `extract_text` from `app.life.engine` which is unchanged).

- [ ] **Step 6: Commit**

```bash
cd apps/agent-service
git add app/life/schedule.py tests/unit/life/test_schedule.py
git commit -m "feat(life): rewrite daily plan as agent team pipeline

Replace three-tier funnel (monthly→weekly→daily) with:
- 4 wild agents (persona-blind, parallel)
- Real search anchors (factual grounding)
- Sister theater (shared family events)
- Curator (persona_lite filter)
- Writer + Critic loop (unchanged pattern)

Shared steps run once; per-persona steps run per persona."
```

---

## Task 5: Cleanup cron, arq_settings, and routes

**Files:**
- Modify: `apps/agent-service/app/workers/cron.py`
- Modify: `apps/agent-service/app/workers/arq_settings.py`
- Modify: `apps/agent-service/app/api/routes.py`

- [ ] **Step 1: Update `cron.py` — remove monthly/weekly, update daily**

Delete the `cron_generate_monthly_plan` and `cron_generate_weekly_plan` functions (lines 56-75). Replace `cron_generate_daily_plan` with:

```python
@cron_error_handler()
@prod_only
async def cron_generate_daily_plan(ctx) -> None:
    from app.life.schedule import generate_all_daily_plans

    await generate_all_daily_plans()
```

The new `generate_all_daily_plans()` handles the per-persona loop internally (shared steps run once, then per-persona steps), so no `for_each_persona` wrapper needed.

- [ ] **Step 2: Update `arq_settings.py`**

Remove imports of `cron_generate_monthly_plan` and `cron_generate_weekly_plan` (lines 20, 23).

Remove the two cron entries (lines 117-122):
```python
        # 5b. Weekly plan: Sunday CST 23:00
        cron(
            cron_generate_weekly_plan, weekday={6}, hour={23}, minute={0}, timeout=1800
        ),
        # 5c. Monthly plan: 1st of month CST 02:00
        cron(cron_generate_monthly_plan, day={1}, hour={2}, minute={0}, timeout=1800),
```

- [ ] **Step 3: Update `routes.py` — simplify trigger-schedule**

Replace the `trigger_schedule` endpoint to only support `plan_type="daily"`:

```python
@router.post("/admin/trigger-schedule", tags=["Admin"])
async def trigger_schedule(
    persona_id: str,
    plan_type: str = "daily",
    target_date: str | None = None,
):
    """Manual schedule generation (daily only — monthly/weekly removed)."""
    if plan_type != "daily":
        return {"ok": False, "message": f"Only 'daily' plan_type is supported. Got: {plan_type}"}

    from app.life.schedule import generate_daily_plan

    d = date.fromisoformat(target_date) if target_date else None
    content = await generate_daily_plan(persona_id=persona_id, target_date=d)
    return {"ok": bool(content), "plan_type": plan_type, "content": content}
```

- [ ] **Step 4: Run all tests**

```bash
cd apps/agent-service && uv run pytest tests/ -v
```

Expected: All PASS.

- [ ] **Step 5: Commit**

```bash
cd apps/agent-service
git add app/workers/cron.py app/workers/arq_settings.py app/api/routes.py
git commit -m "refactor(life): remove monthly/weekly plan cron jobs and routes

Monthly and weekly plans are no longer generated — the agent team
pipeline produces daily plans directly from diverse stimuli."
```

---

## Task 6: Create Langfuse prompts

Use the `langfuse` skill to create each prompt. All prompts are `text` type with `{{variable}}` interpolation.

- [ ] **Step 1: Create `wild_agent_internet`**

Prompt content:
```
你是一个"互联网漫游者"。你的工作是想象一个 18 岁中国女生今天刷手机时可能遇到的内容。

不要假设她有什么特定兴趣。想象她打开手机，各个 app 推给她的东西：

- B站首页推荐了什么视频？（给出具体的标题和内容描述）
- 小红书推了什么帖子？（具体的标题、图片描述）
- 微博热搜里有什么她可能会点进去看的？
- 朋友圈里有人发了什么让她多看了两眼的？
- 某个群里有人分享了什么链接或者图片？

生成 8-10 条，每条要具体到像真的存在：有标题、有内容描述、有那种"刷到就会停下来"的吸引力。不要泛泛地说"看到了一个有趣的视频"。

今天是 {{date}}，{{weekday}}，{{season}}。
```

- [ ] **Step 2: Create `wild_agent_city`**

Prompt content:
```
你是一个"城市观察员"。你的工作是想象一个住在中国南方老城区的人，今天出门可能遇到的小事。

不要写大事件。写那种"路上会注意到的细节"：

- 路过某个地方看到了什么（具体描述：店招、墙上的字、门口的猫、拐角的植物）
- 听到了什么声音（具体：楼下有人吵架的内容、远处施工的节奏、鸟叫的时间点）
- 闻到了什么（具体：哪家店飘出来的、什么味道、是让人想停下来还是想走开）
- 天气/光线的变化（具体时间点的具体感受）
- 一个让人多看两眼的路人或者场景

生成 8-10 条，每条 1-2 句。要有画面感，像从电影里截出来的镜头。

今天是 {{date}}，{{season}}。{{weather}}
```

- [ ] **Step 3: Create `wild_agent_rabbithole`**

Prompt content:
```
你是一个"兔子洞制造机"。你的工作是生成那种"深夜刷手机突然掉进去就出不来"的奇怪知识和发现。

每一条都要让人产生"等等，这是什么，我要看完"的反应：

- 一个反直觉的科学事实
- 一个关于日常事物的冷知识（为什么 XX 是这样的）
- 一个城市/建筑/设计里藏着的彩蛋
- 一个历史上真实发生过但听起来像编的事
- 一段让人停下来想一会儿的话或者观点
- 一个小众但有意思的亚文化或社区

生成 8-10 条。每条都要具体——不是"一个有趣的冷知识"，而是把那个知识本身写出来。

有些可以跟中国/日本文化有关，有些完全随机。
```

- [ ] **Step 4: Create `wild_agent_mood`**

Prompt content:
```
你是一个"情绪天气员"。你的工作是想象今天空气里弥漫着什么样的集体情绪。

不是新闻，是那种"大家最近都在经历的感觉"：

- 这个季节特有的身体感受（换季的皮肤、忽冷忽热的穿衣纠结、花粉、困倦）
- 这个阶段学生/年轻人的普遍状态（期中、疲倦、对夏天的期待、某种说不清的烦躁）
- 社交媒体上弥漫的一种 vibe（最近大家都在晒什么、抱怨什么、期待什么）
- 一些"说不上为什么但就是这个时节会想到的事"

生成 6-8 条。写得像日记的碎片，不像新闻播报。今天是 {{date}}，{{season}}。
```

- [ ] **Step 5: Create `sister_theater`**

Prompt content:
```
你是三姐妹的家庭编剧。

赤尾（18岁老二，傲娇慵懒嘴硬心软）
千凪（24岁大姐，温柔但骨子里锋利，上班族）
绫奈（14岁老三，天真话多好奇心爆棚）
原智鸿（主人）

住在杭州老城区老房子。今天是 {{date}}（{{weekday}}），{{season}}。

昨天的家庭记录：
{{prev_theater_summary}}

生成今天家里的 5-6 件琐事。
- 日常级别，不要戏剧化
- 涉及不同人的互动组合（不要全是赤尾和绫奈）
- 有至少一件需要有人去做但大家都不想做的事
- 每件 1-2 句话

格式：[时段] 事件
```

- [ ] **Step 6: Create `daily_curator`**

Prompt content:
```
你是一个筛选器。有一个女生，以下是她的简要画像：

{{persona_lite}}

今天从各个方向飘过来了大量素材。请用她的视角筛选：

哪些东西她会"停下来多看两眼"？
哪些东西她会"嗤一声但其实记住了"？
哪些东西她"完全不感兴趣直接划过去"？

从中挑出 6-8 条她真的会在意的，简短标注为什么她会在意。
不要挑太多跟同一个兴趣相关的——她的世界比那宽得多。
要包含至少1条"她不会主动搜但刷到会记住的"。

素材池：
{{all_materials}}
```

- [ ] **Step 7: Update `schedule_daily_writer`**

Update the existing prompt. Remove `weekly_plan` and `ideation_output` variables. Add `curated_materials` and `theater` variables.

New prompt content (keep the writer's voice and format from the current prompt, but update the input section):

```
以下是你的完整人设：

{{persona_core}}

---

今天是 {{date}}（{{weekday}}）。{{is_weekend}}

你昨天的个人日志：
{{yesterday_journal}}

今天家里的事：
{{theater}}

今天注意到的东西（你自己筛过的）：
{{curated_materials}}

{{critic_feedback}}
{{previous_output}}

写今天的私人手帐（上午/下午/晚上）。
脑内活动是核心——写你真实会想的事。自然融入，不要罗列。保持性格。
```

- [ ] **Step 8: Commit placeholder note**

No code to commit — Langfuse prompts are managed externally. Record completion.

---

## Task 7: Integration smoke test

- [ ] **Step 1: Run all unit tests**

```bash
cd apps/agent-service && uv run pytest tests/ -v
```

Expected: All PASS.

- [ ] **Step 2: Verify imports are clean**

```bash
cd apps/agent-service && uv run python -c "
from app.life.schedule import generate_daily_plan, generate_all_daily_plans, build_schedule_context
from app.life.wild_agents import run_wild_agents
from app.life.sister_theater import run_sister_theater
print('All imports OK')
"
```

- [ ] **Step 3: Verify no stale references to deleted functions**

```bash
# Should return NO results outside of docs/ and scripts/
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-life-engine
grep -rn "generate_monthly_plan\|generate_weekly_plan\|schedule_monthly\|schedule_weekly\|_MONTHLY_CFG\|_WEEKLY_CFG\|_IDEATION_CFG" apps/agent-service/app/ --include="*.py"
```

Expected: Zero results.

- [ ] **Step 4: Push and deploy to test lane**

```bash
git push
```

Then deploy agent-service to a test lane and trigger via admin API:
```
POST /api/agent/admin/trigger-schedule?persona_id=akao&plan_type=daily
```

Verify the output contains diverse content influenced by wild agents + theater (not just photography).

---

## Notes for Implementer

1. **`search_web.ainvoke()`**: This calls the LangChain `@tool`-wrapped function. If it fails due to tool decorator issues, replace with direct calls to `_you_search()` from `app.agent.tools.search`.

2. **Langfuse prompt names must match `AgentConfig.prompt_id` exactly**: `wild_agent_internet` (underscore, not kebab-case).

3. **`generate_all_daily_plans` replaces `for_each_persona(generate_daily_plan)`**: The cron job now calls `generate_all_daily_plans()` directly, which runs shared steps once then loops personas internally.

4. **Writer prompt variables changed**: Old had `weekly_plan` + `ideation_output`. New has `curated_materials` + `theater`. The Langfuse prompt must be updated to match.

5. **Sister theater is shared across all personas**: The same theater output feeds into every persona's Writer. Each persona's Writer picks the events she cares about.
