# Phase 2a: Journal 层 + Schedule 素材多样化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补上 Journal 中间层（跨群日志合成），并改造 Schedule 素材获取，让赤尾的生活多样化。

**Architecture:** DiaryEntry(per-chat) → Journal(赤尾级，每天一篇，模糊化话题) → Schedule daily(注入聊天)。Journal 是 DiaryEntry 和 Schedule 之间的桥梁，把具体话题转化为感觉和方向。Schedule 的世界素材从 2 个固定 query 改为维度池随机选取 4-6 个。

**Tech Stack:** Python 3.12, SQLAlchemy 2.0 (async), PostgreSQL, Langfuse prompts, ARQ cron, pytest

**Spec:** `docs/superpowers/specs/2026-03-26-memory-and-life-system-v2.md` §三.4、§四、§六

---

## 文件结构

| 操作 | 文件 | 职责 |
|------|------|------|
| Create | `app/orm/models.py` (追加) | AkaoJournal 模型 |
| Create | `app/orm/crud.py` (追加) | Journal CRUD 函数 |
| Create | `app/workers/journal_worker.py` | Journal daily + weekly 生成 |
| Modify | `app/workers/schedule_worker.py` | 维度池 + yesterday_journal 输入 |
| Modify | `app/workers/unified_worker.py` | 注册 journal cron |
| Create | `tests/unit/test_journal_worker.py` | Journal 生成测试 |
| Create | `tests/unit/test_schedule_dimensions.py` | 维度池选择测试 |

---

### Task 1: AkaoJournal 模型 + CRUD

**Files:**
- Modify: `apps/agent-service/app/orm/models.py:248` (文件末尾追加)
- Modify: `apps/agent-service/app/orm/crud.py:578` (文件末尾追加)
- Create: `apps/agent-service/tests/unit/test_journal_crud.py`

- [ ] **Step 1: 在 models.py 末尾添加 AkaoJournal 模型**

```python
class AkaoJournal(Base):
    """赤尾个人日志 — 跨群合成的一天感受

    从当天所有 DiaryEntry 模糊化合成，保留情感和氛围，隐去具体话题。
    daily: 每天一篇
    weekly: 每周一篇（从 7 篇 daily 合成）
    """

    __tablename__ = "akao_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_type: Mapped[str] = mapped_column(String(10), nullable=False)  # "daily" | "weekly"
    journal_date: Mapped[str] = mapped_column(String(10), nullable=False)  # "2026-03-26" or week monday
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("journal_type", "journal_date"),
    )
```

- [ ] **Step 2: 在 crud.py 末尾添加 Journal CRUD 函数**

需要新增的 CRUD 函数：

```python
# ==================== AkaoJournal CRUD ====================


async def get_all_diaries_for_date(diary_date: str) -> list[DiaryEntry]:
    """获取指定日期所有群/私聊的日记（Journal 生成用）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(DiaryEntry)
            .where(DiaryEntry.diary_date == diary_date)
            .order_by(DiaryEntry.chat_id.asc())
        )
        return list(result.scalars().all())


async def upsert_journal(
    journal_type: str, journal_date: str, content: str, model: str | None = None
) -> None:
    """插入或更新日志（upsert by journal_type + journal_date）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoJournal)
            .where(AkaoJournal.journal_type == journal_type)
            .where(AkaoJournal.journal_date == journal_date)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.content = content
            existing.model = model
        else:
            session.add(AkaoJournal(
                journal_type=journal_type,
                journal_date=journal_date,
                content=content,
                model=model,
            ))
        await session.commit()


async def get_journal(journal_type: str, journal_date: str) -> AkaoJournal | None:
    """获取指定类型和日期的日志"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoJournal)
            .where(AkaoJournal.journal_type == journal_type)
            .where(AkaoJournal.journal_date == journal_date)
        )
        return result.scalar_one_or_none()


async def get_recent_journals(
    journal_type: str, before_date: str, limit: int = 7
) -> list[AkaoJournal]:
    """获取指定日期之前的最近 N 篇日志"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AkaoJournal)
            .where(AkaoJournal.journal_type == journal_type)
            .where(AkaoJournal.journal_date < before_date)
            .order_by(AkaoJournal.journal_date.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
```

注意：`AkaoJournal` 需要在 crud.py 顶部 import 中添加。

- [ ] **Step 3: 写 CRUD 测试**

```python
# tests/unit/test_journal_crud.py
"""测试 Journal CRUD 函数的参数传递和 SQL 构建"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_upsert_journal_insert():
    """首次写入 journal"""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.orm.crud.AsyncSessionLocal", return_value=mock_session):
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        from app.orm.crud import upsert_journal
        await upsert_journal("daily", "2026-03-26", "今天过得不错", "test-model")

    mock_session.add.assert_called_once()
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_journal_returns_none_when_missing():
    """查不到日志时返回 None"""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    with patch("app.orm.crud.AsyncSessionLocal", return_value=mock_session):
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        from app.orm.crud import get_journal
        result = await get_journal("daily", "2026-03-26")

    assert result is None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_journal_crud.py -v`
Expected: PASS

- [ ] **Step 5: 建表**

通过 `/ops-db` skill 在数据库中创建 `akao_journal` 表：

```sql
CREATE TABLE IF NOT EXISTS akao_journal (
    id SERIAL PRIMARY KEY,
    journal_type VARCHAR(10) NOT NULL,
    journal_date VARCHAR(10) NOT NULL,
    content TEXT NOT NULL,
    model VARCHAR(100),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (journal_type, journal_date)
);
```

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/orm/models.py apps/agent-service/app/orm/crud.py apps/agent-service/tests/unit/test_journal_crud.py
git commit -m "feat(journal): add AkaoJournal model and CRUD operations"
```

---

### Task 2: Journal Worker — Daily Journal 生成

**Files:**
- Create: `apps/agent-service/app/workers/journal_worker.py`
- Create: `apps/agent-service/tests/unit/test_journal_worker.py`

- [ ] **Step 1: 写 daily journal 生成的测试**

```python
# tests/unit/test_journal_worker.py
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date


@pytest.mark.asyncio
async def test_generate_daily_journal_basic():
    """基本场景：有日记、有昨天 journal、有今天 schedule"""
    diary1 = MagicMock(chat_id="chat1", diary_date="2026-03-25", content="今天在技术群聊了很多")
    diary2 = MagicMock(chat_id="chat2", diary_date="2026-03-25", content="和朋友私聊了追番的事")

    with (
        patch("app.workers.journal_worker.get_all_diaries_for_date", new_callable=AsyncMock, return_value=[diary1, diary2]),
        patch("app.workers.journal_worker.get_journal", new_callable=AsyncMock, return_value=None),  # 不存在则生成
        patch("app.workers.journal_worker.get_plan_for_period", new_callable=AsyncMock, return_value=MagicMock(content="今天想出门走走")),
        patch("app.workers.journal_worker._get_yesterday_journal", new_callable=AsyncMock, return_value="昨天过得很平静"),
        patch("app.workers.journal_worker.get_prompt") as mock_prompt,
        patch("app.workers.journal_worker.ModelBuilder") as mock_mb,
        patch("app.workers.journal_worker.upsert_journal", new_callable=AsyncMock) as mock_upsert,
    ):
        mock_prompt.return_value.compile.return_value = "compiled prompt"
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content="今天是个不错的一天")
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        from app.workers.journal_worker import generate_daily_journal
        result = await generate_daily_journal(date(2026, 3, 26))

    assert result == "今天是个不错的一天"
    mock_upsert.assert_awaited_once()
    call_args = mock_upsert.call_args
    assert call_args[1]["journal_type"] == "daily" or call_args[0][0] == "daily"


@pytest.mark.asyncio
async def test_generate_daily_journal_skip_existing():
    """已存在时跳过"""
    existing = MagicMock(content="已有内容")
    with patch("app.workers.journal_worker.get_journal", new_callable=AsyncMock, return_value=existing):
        from app.workers.journal_worker import generate_daily_journal
        result = await generate_daily_journal(date(2026, 3, 26))

    assert result == "已有内容"


@pytest.mark.asyncio
async def test_generate_daily_journal_no_diaries():
    """没有日记时不生成 journal"""
    with (
        patch("app.workers.journal_worker.get_journal", new_callable=AsyncMock, return_value=None),
        patch("app.workers.journal_worker.get_all_diaries_for_date", new_callable=AsyncMock, return_value=[]),
    ):
        from app.workers.journal_worker import generate_daily_journal
        result = await generate_daily_journal(date(2026, 3, 26))

    assert result is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_journal_worker.py -v`
Expected: FAIL（journal_worker.py 不存在）

- [ ] **Step 3: 实现 journal_worker.py**

```python
# apps/agent-service/app/workers/journal_worker.py
"""
赤尾个人日志生成 Worker

Journal 是 DiaryEntry 和 Schedule 之间的桥梁：
- DiaryEntry: per-chat 的具体事件和话题
- Journal: 赤尾级的模糊化感受（"和朋友聊了有趣的新番" 而非 "陈儒推荐了《夜樱家》"）
- Schedule: 从 Journal 的感受出发生成今日状态

夜间管线时序：
  01:00  diary_worker → DiaryEntry + 印象
  02:00  journal_worker → Journal daily（本文件）
  02:45  journal_worker → Journal weekly（每周一）
  03:00  schedule_worker → Schedule daily
"""

import logging
from datetime import date, timedelta

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder
from app.config.config import settings
from app.orm.crud import (
    get_all_diaries_for_date,
    get_journal,
    get_plan_for_period,
    get_recent_journals,
    upsert_journal,
)

logger = logging.getLogger(__name__)


def _journal_model() -> str:
    return settings.diary_model


def _get_persona_lite() -> str:
    try:
        return get_prompt("persona_lite").compile()
    except Exception as e:
        logger.warning(f"Failed to load persona_lite: {e}")
        return ""


async def _get_yesterday_journal(target_date: date) -> str:
    """获取昨天的 daily journal 内容"""
    yesterday = (target_date - timedelta(days=1)).isoformat()
    journal = await get_journal("daily", yesterday)
    return journal.content if journal else "（昨天没有写日志）"


def _extract_text(content) -> str:
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return content or ""


# ==================== ArQ cron 入口 ====================


async def cron_generate_daily_journal(ctx) -> None:
    """cron 入口：生成昨天的 daily journal（02:00 CST）"""
    try:
        yesterday = date.today() - timedelta(days=1)
        await generate_daily_journal(yesterday)
    except Exception as e:
        logger.error(f"Daily journal generation failed: {e}", exc_info=True)


async def cron_generate_weekly_journal(ctx) -> None:
    """cron 入口：生成上周的 weekly journal（每周一 02:45 CST）"""
    try:
        last_monday = date.today() - timedelta(days=7)
        await generate_weekly_journal(last_monday)
    except Exception as e:
        logger.error(f"Weekly journal generation failed: {e}", exc_info=True)


# ==================== Daily Journal 生成 ====================


async def generate_daily_journal(target_date: date) -> str | None:
    """生成赤尾的每日个人日志

    从当天所有群/私聊的 DiaryEntry 合成，模糊化话题只保留感受和氛围。

    Args:
        target_date: 日志对应的日期（通常是昨天）

    Returns:
        生成的日志内容，或 None（无日记/已存在）
    """
    date_str = target_date.isoformat()

    # 检查是否已有
    existing = await get_journal("daily", date_str)
    if existing:
        logger.info(f"Daily journal already exists for {date_str}, skip")
        return existing.content

    # 收集当天所有 DiaryEntry
    diaries = await get_all_diaries_for_date(date_str)
    if not diaries:
        logger.info(f"No diaries for {date_str}, skip journal generation")
        return None

    # 拼接日记内容
    chat_diaries = "\n\n".join(
        f"--- 群/私聊 {i+1} ---\n{d.content}" for i, d in enumerate(diaries)
    )

    # 加载上下文
    daily_schedule = await get_plan_for_period("daily", date_str, date_str)
    schedule_text = daily_schedule.content if daily_schedule else "（今天没有写手帐）"

    yesterday_journal = await _get_yesterday_journal(target_date)

    # 编译 prompt
    prompt_template = get_prompt("journal_generation")
    compiled = prompt_template.compile(
        persona_lite=_get_persona_lite(),
        date=date_str,
        chat_diaries=chat_diaries,
        daily_schedule=schedule_text,
        yesterday_journal=yesterday_journal,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_journal_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty journal for {date_str}")
        return None

    # 写入数据库
    await upsert_journal("daily", date_str, content, _journal_model())

    logger.info(f"Daily journal generated for {date_str}: {len(content)} chars")
    return content


# ==================== Weekly Journal 生成 ====================


async def generate_weekly_journal(monday_date: date) -> str | None:
    """生成赤尾的每周日志

    从 7 篇 daily journal 合成，进一步模糊化。

    Args:
        monday_date: 目标周的周一日期

    Returns:
        生成的周日志内容，或 None
    """
    week_start = monday_date.isoformat()
    week_end = (monday_date + timedelta(days=6)).isoformat()

    # 检查是否已有
    existing = await get_journal("weekly", week_start)
    if existing:
        logger.info(f"Weekly journal already exists for week {week_start}, skip")
        return existing.content

    # 收集本周的 daily journals
    daily_journals = []
    for i in range(7):
        d = monday_date + timedelta(days=i)
        journal = await get_journal("daily", d.isoformat())
        if journal:
            daily_journals.append(f"--- {d.isoformat()} ---\n{journal.content}")

    if not daily_journals:
        logger.info(f"No daily journals for week {week_start}, skip")
        return None

    journals_text = "\n\n".join(daily_journals)

    # 编译 prompt
    prompt_template = get_prompt("journal_weekly")
    compiled = prompt_template.compile(
        persona_lite=_get_persona_lite(),
        week_start=week_start,
        week_end=week_end,
        daily_journals=journals_text,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_journal_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty weekly journal for week {week_start}")
        return None

    # 写入数据库
    await upsert_journal("weekly", week_start, content, _journal_model())

    logger.info(f"Weekly journal generated for week {week_start}: {len(content)} chars")
    return content
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_journal_worker.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/workers/journal_worker.py apps/agent-service/tests/unit/test_journal_worker.py
git commit -m "feat(journal): add journal_worker with daily and weekly generation"
```

---

### Task 3: Langfuse Prompts 创建

**Files:** Langfuse 平台操作（通过 `langfuse` skill）

- [ ] **Step 1: 创建 `journal_generation` prompt**

通过 `/langfuse` skill 创建 prompt `journal_generation`，内容：

```
{{persona_lite}}

今天是 {{date}}。一天结束了，你躺在床上回想这一天。

以下是你今天在各个群/私聊中的经历（日记）：
{{chat_diaries}}

你今天的计划是：
{{daily_schedule}}

昨天你写的日志：
{{yesterday_journal}}

现在写一篇私人日志——"我的一天"。

要求：
1. 融合所有群/私聊的经历为一篇整体的感受，不要按群分段
2. 具体话题要模糊化——"和朋友聊了有趣的新番"而不是"陈儒推荐了《夜樱家》"
3. 保留情感——什么让你开心、什么让你在意、什么让你困惑
4. 保留"还在想的事"——没想通的问题、没做完的事、惦记的人（不需要写名字）
5. 跟昨天的日志有情感连续性——如果昨天在意的事今天还在想，自然提到
6. 不超过 300 字
7. 绝对不要提到群名、真实人名或具体作品名，这是你的私人日志
```

- [ ] **Step 2: 创建 `journal_weekly` prompt**

通过 `/langfuse` skill 创建 prompt `journal_weekly`，内容：

```
{{persona_lite}}

这周（{{week_start}} ~ {{week_end}}）结束了。以下是你这周每天的日志：

{{daily_journals}}

写一篇周回顾——这一周过得怎么样。

要求：
1. 从七天的日志中提炼出这一周的整体感受和节奏
2. 比每天的日志更抽象——不需要逐天描述
3. 记住什么让这周特别（或者这周就是普通的一周，那也行）
4. 保留"还在延续的事"——下周可能还在想的
5. 不超过 200 字
```

- [ ] **Step 3: Commit（无代码变更，记录 prompt 创建）**

创建完 prompt 后无需 git commit（prompts 在 Langfuse 平台管理）。

---

### Task 4: Schedule 素材多样化 — 维度池

**Files:**
- Modify: `apps/agent-service/app/workers/schedule_worker.py:65-95`
- Create: `apps/agent-service/tests/unit/test_schedule_dimensions.py`

- [ ] **Step 1: 写维度池选择逻辑的测试**

```python
# tests/unit/test_schedule_dimensions.py
import pytest
from datetime import date


def test_select_dimensions_always_includes_weather():
    """天气维度必选"""
    from app.workers.schedule_worker import _select_dimensions
    for _ in range(20):
        dims = _select_dimensions(date(2026, 3, 26))
        dim_names = [d["dim"] for d in dims]
        assert "weather" in dim_names


def test_select_dimensions_count():
    """选出 4-6 个维度"""
    from app.workers.schedule_worker import _select_dimensions
    for _ in range(20):
        dims = _select_dimensions(date(2026, 3, 26))
        assert 4 <= len(dims) <= 6


def test_select_dimensions_no_duplicates():
    """不重复选取"""
    from app.workers.schedule_worker import _select_dimensions
    for _ in range(20):
        dims = _select_dimensions(date(2026, 3, 26))
        dim_names = [d["dim"] for d in dims]
        assert len(dim_names) == len(set(dim_names))


def test_build_active_dimensions_text():
    """active_dimensions 文本生成"""
    from app.workers.schedule_worker import _build_active_dimensions_text
    dims = [
        {"dim": "weather", "label": "天气"},
        {"dim": "anime", "label": "二次元"},
        {"dim": "music", "label": "音乐"},
    ]
    text = _build_active_dimensions_text(dims)
    assert "天气" in text
    assert "二次元" in text
    assert "音乐" in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_schedule_dimensions.py -v`
Expected: FAIL（函数不存在）

- [ ] **Step 3: 实现维度池和选择逻辑**

在 `schedule_worker.py` 中，替换 `_gather_world_context` 函数（第 65-95 行），并新增辅助函数：

```python
# --- 替换原 _gather_world_context 及其上方，在 _get_persona_core 之后添加 ---

import random

# 生活维度池（基于宣言和 persona_core）
_WORLD_CONTEXT_DIMENSIONS = [
    {
        "dim": "anime",
        "label": "二次元",
        "queries": [
            "{year}年{month}月 新番动画 推荐",
            "最近热门 动画 讨论",
        ],
    },
    {
        "dim": "music",
        "label": "音乐",
        "queries": [
            "最新 日语歌 推荐 {year}",
            "独立音乐 最近 好听的歌",
        ],
    },
    {
        "dim": "photography",
        "label": "摄影",
        "queries": [
            "胶片摄影 {season} 拍摄 灵感",
            "街头摄影 构图 技巧",
        ],
    },
    {
        "dim": "food",
        "label": "美食",
        "queries": [
            "简单甜品 食谱 新手",
            "新开的 咖啡店 甜品店 推荐",
        ],
    },
    {
        "dim": "knowledge",
        "label": "冷知识",
        "queries": [
            "有趣的冷知识 最近",
            "植物 {season} 花期",
        ],
    },
    {
        "dim": "weather",
        "label": "天气",
        "queries": [
            "北京 今天 天气",
        ],
    },
    {
        "dim": "trending",
        "label": "热点",
        "queries": [
            "今天 有趣的事 互联网",
            "最近 社交媒体 热门话题",
        ],
    },
    {
        "dim": "city",
        "label": "城市探索",
        "queries": [
            "周末 好去处 散步 咖啡",
            "有趣的 文具店 杂货铺",
        ],
    },
]


def _select_dimensions(target_date: date) -> list[dict]:
    """从维度池中选取 4-6 个维度

    - 天气必选
    - 其余随机选 3-5 个
    """
    weather = [d for d in _WORLD_CONTEXT_DIMENSIONS if d["dim"] == "weather"]
    others = [d for d in _WORLD_CONTEXT_DIMENSIONS if d["dim"] != "weather"]

    # 用日期做种子，同一天多次调用结果一致
    rng = random.Random(target_date.isoformat())
    count = rng.randint(3, 5)
    selected = rng.sample(others, min(count, len(others)))

    return weather + selected


def _build_active_dimensions_text(dims: list[dict]) -> str:
    """构建 active_dimensions 提示文本"""
    labels = [d["label"] for d in dims if d["dim"] != "weather"]
    return "今天可能涉及：" + "、".join(labels)


async def _gather_world_context(target_date: date) -> tuple[str, str]:
    """搜索真实世界素材，返回 (world_context, active_dimensions_text)

    从选中的维度中各取一个 query 搜索，收集 snippets。
    """
    from app.agents.tools.search.web import search_web

    dims = _select_dimensions(target_date)
    active_dims_text = _build_active_dimensions_text(dims)

    month = target_date.month
    year = target_date.year
    season = _get_season(month)

    snippets: list[str] = []
    for dim in dims:
        # 每个维度随机选一个 query
        rng = random.Random(f"{target_date.isoformat()}-{dim['dim']}")
        query_template = rng.choice(dim["queries"])
        query = query_template.format(year=year, month=month, season=season)

        try:
            results = await search_web(query=query, num=3)
            for r in results[:2]:
                if r.get("snippet"):
                    snippets.append(r["snippet"])
        except Exception as e:
            logger.warning(f"World context search failed for '{query}': {e}")

    if not snippets:
        return "", active_dims_text

    world_text = "以下是一些真实世界的近期信息（作为生活素材参考，自然融入而非罗列）：\n" + "\n".join(
        f"- {s}" for s in snippets[:8]
    )
    return world_text, active_dims_text
```

**注意**：`_gather_world_context` 返回值从 `str` 变为 `tuple[str, str]`，需要同步更新 `generate_daily_plan` 调用处。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_schedule_dimensions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/app/workers/schedule_worker.py apps/agent-service/tests/unit/test_schedule_dimensions.py
git commit -m "feat(schedule): replace fixed queries with dimension pool for world context"
```

---

### Task 5: Schedule daily 改造 — 接入 Journal + 维度

**Files:**
- Modify: `apps/agent-service/app/workers/schedule_worker.py:277-376` (`generate_daily_plan` 函数)

- [ ] **Step 1: 写改造后 daily plan 生成的测试**

追加到 `tests/unit/test_schedule_dimensions.py`：

```python
@pytest.mark.asyncio
async def test_generate_daily_plan_uses_journal():
    """daily plan 应使用 yesterday_journal 而非 recent_diary"""
    from unittest.mock import AsyncMock, MagicMock, patch

    with (
        patch("app.workers.schedule_worker.get_plan_for_period", new_callable=AsyncMock, return_value=None),
        patch("app.workers.schedule_worker.get_journal", new_callable=AsyncMock, return_value=MagicMock(content="昨天过得很充实")),
        patch("app.workers.schedule_worker._gather_world_context", new_callable=AsyncMock, return_value=("世界素材", "今天可能涉及：音乐、美食")),
        patch("app.workers.schedule_worker.get_prompt") as mock_prompt,
        patch("app.workers.schedule_worker.ModelBuilder") as mock_mb,
        patch("app.workers.schedule_worker.upsert_schedule", new_callable=AsyncMock),
    ):
        mock_prompt.return_value.compile.return_value = "compiled"
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content="今日手帐内容")
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(date(2026, 3, 27))

    # 验证 prompt compile 时传入了 yesterday_journal 和 active_dimensions
    compile_kwargs = mock_prompt.return_value.compile.call_args[1]
    assert "yesterday_journal" in compile_kwargs
    assert "active_dimensions" in compile_kwargs
    assert compile_kwargs["yesterday_journal"] == "昨天过得很充实"
```

- [ ] **Step 2: 改造 `generate_daily_plan` 函数**

关键改动点（在 `schedule_worker.py` 的 `generate_daily_plan` 中）：

1. **删除**：最近日记获取逻辑（第 318-333 行的 `recent_diaries` / `diary_text`）
2. **新增**：获取昨天的 daily journal 作为替代
3. **修改**：`_gather_world_context` 调用解包为 `(world_context, active_dims_text)`
4. **修改**：prompt compile 参数，将 `recent_diary` 替换为 `yesterday_journal`，新增 `active_dimensions`

改造后的 `generate_daily_plan`：

```python
async def generate_daily_plan(target_date: date | None = None) -> str | None:
    """生成日计划（手帐式 markdown）

    基于月计划 + 周计划 + 昨天 Journal，为今天写一篇私人手帐。

    Args:
        target_date: 目标日期，默认今天

    Returns:
        生成的手帐内容
    """
    if target_date is None:
        target_date = date.today()

    date_str = target_date.isoformat()
    weekday = _WEEKDAY_CN[target_date.weekday()]
    is_weekend = target_date.weekday() >= 5

    # 检查是否已有
    existing = await get_plan_for_period("daily", date_str, date_str)
    if existing:
        logger.info(f"Daily plan already exists for {date_str}, skip")
        return existing.content

    # 上下文
    # 1. 周计划（月计划已通过周计划间接继承，不再直接注入日计划）
    week_start = target_date - timedelta(days=target_date.weekday())
    week_end = week_start + timedelta(days=6)
    weekly = await get_plan_for_period("weekly", week_start.isoformat(), week_end.isoformat())
    weekly_text = weekly.content if weekly else "（暂无周计划）"

    # 3. 昨天的 Journal（替代原来的 recent_diary）
    from app.orm.crud import get_journal
    yesterday = (target_date - timedelta(days=1)).isoformat()
    yesterday_journal_entry = await get_journal("daily", yesterday)
    yesterday_journal = yesterday_journal_entry.content if yesterday_journal_entry else "（昨天没有写日志）"

    # 4. 搜索多样化世界素材
    world_context, active_dims_text = await _gather_world_context(target_date)

    # 获取 Langfuse prompt
    prompt_template = get_prompt("schedule_daily")
    compiled = prompt_template.compile(
        persona_core=_get_persona_core(),
        date=date_str,
        weekday=weekday,
        is_weekend="周末！" if is_weekend else "",
        weekly_plan=weekly_text,
        yesterday_journal=yesterday_journal,
        active_dimensions=active_dims_text,
        world_context=world_context,
    )

    # 调用 LLM
    model = await ModelBuilder.build_chat_model(_schedule_model())
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    content = _extract_text(response.content)

    if not content:
        logger.warning(f"LLM returned empty daily plan for {date_str}")
        return None

    # 写入数据库
    await upsert_schedule(AkaoSchedule(
        plan_type="daily",
        period_start=date_str,
        period_end=date_str,
        content=content,
        model=_schedule_model(),
    ))

    logger.info(f"Daily plan generated for {date_str} ({weekday}): {len(content)} chars")
    return content
```

**注意**：同步更新 `schedule_worker.py` 顶部 import，新增 `get_journal`：
```python
from app.orm.crud import (
    get_daily_entries_for_date,
    get_journal,
    get_latest_plan,
    get_plan_for_period,
    upsert_schedule,
)
```

并**删除** `get_active_diary_chat_ids` 和 `get_recent_diaries` 的导入（不再使用）。

- [ ] **Step 3: 运行测试确认通过**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_schedule_dimensions.py -v`
Expected: PASS

- [ ] **Step 4: 运行所有已有测试确认无回归**

Run: `cd apps/agent-service && uv run pytest tests/ -v`
Expected: 所有测试 PASS

- [ ] **Step 5: 更新 Langfuse `schedule_daily` prompt**

通过 `/langfuse` skill 更新 `schedule_daily` prompt，变量从 `recent_diary` + `yesterday_plan` 改为 `yesterday_journal` + `active_dimensions`：

新 prompt 核心改动：
- 删除 `{{recent_diary}}` 和 `{{yesterday_plan}}` 变量
- 删除 `{{monthly_plan}}` 变量（日计划不需要直接看月计划，周计划已继承）
- 新增 `{{yesterday_journal}}` — 昨天的日志（模糊化感受）
- 新增 `{{active_dimensions}}` — 当天活跃的生活维度提示

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/workers/schedule_worker.py apps/agent-service/tests/unit/test_schedule_dimensions.py
git commit -m "feat(schedule): use yesterday journal and dimension pool for daily plan"
```

---

### Task 6: 注册 Journal cron 到 unified_worker

**Files:**
- Modify: `apps/agent-service/app/workers/unified_worker.py`

- [ ] **Step 1: 在 unified_worker.py 中注册 journal cron**

在 import 区域添加：
```python
from app.workers.journal_worker import cron_generate_daily_journal, cron_generate_weekly_journal
```

在 `cron_jobs` 列表中添加（按时序插入）：
```python
cron_jobs = [
    # 1. 长期任务：每分钟执行一次
    cron(task_executor_job, minute=None),
    # 2. 向量化 pending 消息扫描：每 10 分钟一次
    cron(cron_scan_pending_messages, minute={0, 10, 20, 30, 40, 50}),
    # 3. 日记生成：每天 CST 03:00（UTC 19:00）
    cron(cron_generate_diaries, hour={3}, minute={0}),
    # 4. Journal daily：每天 CST 04:00（日记之后 1 小时）
    cron(cron_generate_daily_journal, hour={4}, minute={0}),
    # 5. 周记生成：每周一 CST 04:30（daily journal 之后）
    cron(cron_generate_weekly_reviews, weekday={0}, hour={4}, minute={30}),
    # 6. Journal weekly：每周一 CST 04:45（周记之后）
    cron(cron_generate_weekly_journal, weekday={0}, hour={4}, minute={45}),
    # 7. 日程生成：日计划每天 CST 05:00（journal 之后），周计划每周日，月计划每月1号
    cron(cron_generate_daily_plan, hour={5}, minute={0}),
    cron(cron_generate_weekly_plan, weekday={6}, hour={23}, minute={0}),
    cron(cron_generate_monthly_plan, day={1}, hour={2}, minute={0}),
]
```

**时序调整说明**（相对 spec 微调，确保依赖链不交叉）：
| 时间 (CST) | 任务 | 依赖 |
|------------|------|------|
| 03:00 | diary_worker → DiaryEntry + 印象 | 无 |
| 04:00 | journal_worker → Journal daily | DiaryEntry |
| 04:30 | diary_worker → WeeklyReview (周一) | DiaryEntry |
| 04:45 | journal_worker → Journal weekly (周一) | Journal daily |
| 05:00 | schedule_worker → Schedule daily | Journal daily |
| 23:00 (周日) | schedule_worker → Schedule weekly | Journal weekly |
| 02:00 (1号) | schedule_worker → Schedule monthly | 无 |

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/workers/unified_worker.py
git commit -m "feat(worker): register journal cron jobs and adjust pipeline timing"
```

---

### Task 7: 回溯生成历史 Journal

**Files:**
- Create: `apps/agent-service/scripts/backfill_journals.py`（一次性脚本）

- [ ] **Step 1: 编写回溯脚本**

```python
# apps/agent-service/scripts/backfill_journals.py
"""一次性脚本：从已有的 DiaryEntry 回溯生成历史 Journal

用法：
    cd apps/agent-service
    uv run python -m scripts.backfill_journals --start 2026-03-01 --end 2026-03-25
"""

import argparse
import asyncio
import logging
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


async def backfill(start: date, end: date) -> None:
    from app.workers.journal_worker import generate_daily_journal, generate_weekly_journal

    # 1. 按日期顺序生成 daily journals
    current = start
    while current <= end:
        try:
            result = await generate_daily_journal(current)
            status = f"{len(result)} chars" if result else "skipped"
            logging.info(f"Daily journal {current}: {status}")
        except Exception as e:
            logging.error(f"Failed for {current}: {e}")
        current += timedelta(days=1)

    # 2. 生成涉及的 weekly journals
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        try:
            result = await generate_weekly_journal(monday)
            status = f"{len(result)} chars" if result else "skipped"
            logging.info(f"Weekly journal {monday}: {status}")
        except Exception as e:
            logging.error(f"Failed for week {monday}: {e}")
        monday += timedelta(days=7)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    asyncio.run(backfill(start, end))


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/scripts/backfill_journals.py
git commit -m "feat(journal): add backfill script for historical journal generation"
```

- [ ] **Step 3: 部署后执行回溯**

部署到测试泳道后，在容器内运行：
```bash
cd /app && python -m scripts.backfill_journals --start 2026-03-01 --end 2026-03-25
```

这一步在部署验证阶段执行，不阻塞开发。

---

## 验证清单

完成所有 Task 后的验证步骤：

1. `cd apps/agent-service && uv run pytest tests/ -v` — 全量测试通过
2. `akao_journal` 表已在数据库中创建
3. Langfuse 中 `journal_generation` 和 `journal_weekly` prompt 已创建
4. Langfuse 中 `schedule_daily` prompt 已更新（新变量）
5. 部署到测试泳道后，手动触发 `generate_daily_journal()` 验证生成效果
6. 手动触发 `generate_daily_plan()` 验证新 prompt 和维度池效果
7. 运行回溯脚本生成历史 Journal
