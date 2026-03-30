# Schedule 多 Agent 管线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `generate_daily_plan()` 从单次 LLM 调用重构为 Ideation → Writer → Critic 三 Agent 管线，解决 Schedule 主题趋同问题。

**Architecture:** Ideation Agent 使用 `langchain.agents.create_agent` + `search_web` 工具运行 tool-use loop；Writer/Critic 使用直接 LLM 调用（无工具不需要 agent loop）。三者的配置（prompt_id, model_id）通过 AgentRegistry 管理。

**Tech Stack:** LangGraph (via `langchain.agents.create_agent`), Langfuse prompts, `offline-model` (gpt-5.4)

---

### Task 1: 注册 AgentConfig

**Files:**
- Modify: `apps/agent-service/app/agents/core/config.py:47-64`

- [ ] **Step 1: 添加 3 个 AgentConfig 注册**

在文件末尾已有的 `AgentRegistry.register` 块后追加：

```python
AgentRegistry.register(
    "schedule-ideation",
    AgentConfig(
        prompt_id="schedule_daily_ideation",
        model_id="offline-model",
        trace_name="schedule-ideation",
    ),
)

AgentRegistry.register(
    "schedule-writer",
    AgentConfig(
        prompt_id="schedule_daily_writer",
        model_id="offline-model",
        trace_name="schedule-writer",
    ),
)

AgentRegistry.register(
    "schedule-critic",
    AgentConfig(
        prompt_id="schedule_daily_critic",
        model_id="offline-model",
        trace_name="schedule-critic",
    ),
)
```

- [ ] **Step 2: 验证注册成功**

Run: `cd apps/agent-service && uv run python3 -c "from app.agents.core.config import AgentRegistry; print(list(AgentRegistry.all_configs().keys()))"`

Expected: `['main', 'research', 'schedule-ideation', 'schedule-writer', 'schedule-critic']`

- [ ] **Step 3: Commit**

```bash
git add apps/agent-service/app/agents/core/config.py
git commit -m "feat(schedule): register 3 agent configs for multi-agent pipeline"
```

---

### Task 2: 创建 Langfuse Prompts

用 langfuse skill 创建 3 个 text 类型 prompt。label 设为 `context-v3`（当前泳道），不动 production。

- [ ] **Step 1: 读取现有 schedule_daily prompt 作为参考**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py get-prompt '{"name":"schedule_daily","label":"production"}'
```

记录当前 Writer 输入的变量名和格式，新的 `schedule_daily_writer` 需要保持输出格式兼容。

- [ ] **Step 2: 创建 schedule_daily_ideation prompt**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py create-prompt '{
  "name": "schedule_daily_ideation",
  "type": "text",
  "prompt": "你是赤尾的\"灵感收集员\"。你的任务是为赤尾今天的日程手帐搜集真实的生活素材。\n\n赤尾是谁：\n{{persona_core}}\n\n昨天她经历了什么：\n{{yesterday_journal}}\n\n她前 3 天的日程（避免雷同）：\n{{recent_schedules}}\n\n今天是 {{date}}（{{weekday}}），{{season}}。\n\n---\n\n用 search_web 工具主动搜索你觉得今天赤尾可能会接触到的东西。\n比如：最近有什么新番上线？她住的城市今天天气怎么样？有没有什么展览/活动？她喜欢的领域有什么新鲜事？\n\n搜什么完全由你决定，但要注意：\n- 搜到的东西要能自然融入一个 19 岁女生的日常，不要硬塞\n- 看看前 3 天用过什么素材，别重复\n- 不需要面面俱到，2-3 个有质感的素材就够了\n\n最后输出你搜集到的素材和灵感，给写作者用。",
  "labels": ["context-v3"]
}'
```

- [ ] **Step 3: 创建 schedule_daily_writer prompt**

基于 Step 1 读取的现有 `schedule_daily` prompt 改写。保持输出格式（手帐式叙事），但输入变量改为接收 Ideation 输出。关键变量：

- `persona_core`, `date`, `weekday`, `is_weekend`, `weekly_plan`, `yesterday_journal` — 与原 prompt 相同
- `ideation_output` — 新增，替代原来的 `world_context` + `active_dimensions`
- `previous_output` — 可选，重写时注入上一版
- `critic_feedback` — 可选，重写时注入审查反馈

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py create-prompt '{
  "name": "schedule_daily_writer",
  "type": "text",
  "prompt": "<在 Step 1 的基础上改写，替换 world_context/active_dimensions 为 ideation_output，末尾加条件重写段>",
  "labels": ["context-v3"]
}'
```

具体 prompt 内容需要基于 Step 1 读取的现有 prompt 适配，此处不硬编码最终文本。

- [ ] **Step 4: 创建 schedule_daily_critic prompt**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py create-prompt '{
  "name": "schedule_daily_critic",
  "type": "text",
  "prompt": "你是赤尾的\"质量审查员\"。以下是她今天的日程手帐，以及她前 3 天的日程：\n\n今天的日程：\n{{today_schedule}}\n\n前 3 天：\n{{recent_schedules}}\n\n请检查：\n1. 读起来像真人写的手帐，还是像 AI 生成的文艺公众号？\n2. 和前 3 天相比，有没有雷同的活动、措辞、意象？\n3. 上午/下午/晚上的状态有变化吗？还是全天一个调子？\n4. 有没有具体的、可触摸的细节？（\"修胶片机螺丝掉了\" vs \"沉浸在光影的世界里\"）\n\n如果都没问题，只输出 PASS。\n如果有问题，输出修改建议（指出具体哪里要改，怎么改）。不要输出 PASS。",
  "labels": ["context-v3"]
}'
```

- [ ] **Step 5: 验证 3 个 prompt 都创建成功**

```bash
python3 ~/.claude/skills/langfuse/scripts/langfuse_api.py list-prompts '{"name":"schedule_daily_"}'
```

Expected: 看到 `schedule_daily_ideation`, `schedule_daily_writer`, `schedule_daily_critic`

---

### Task 3: 写 pipeline 测试（红）

**Files:**
- Create: `apps/agent-service/tests/unit/test_schedule_pipeline.py`

- [ ] **Step 1: 写 pipeline 编排测试**

```python
# tests/unit/test_schedule_pipeline.py
"""Schedule multi-agent pipeline tests"""
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

# 公共常量
FAKE_DATE = date(2026, 4, 1)
FAKE_PERSONA = "赤尾人设"
FAKE_WEEKLY = "本周计划内容"
FAKE_JOURNAL = "昨天的日志"
FAKE_RECENT = [
    MagicMock(content="3月31日手帐", period_start="2026-03-31"),
    MagicMock(content="3月30日手帐", period_start="2026-03-30"),
    MagicMock(content="3月29日手帐", period_start="2026-03-29"),
]
FAKE_IDEATION = "素材：4月新番列表、樱花季展览"
FAKE_SCHEDULE = "今日手帐内容"


async def _mock_get_plan(plan_type, start, end):
    """按 plan_type 区分：daily 返回 None（不存在），weekly 返回周计划"""
    if plan_type == "weekly":
        return MagicMock(content=FAKE_WEEKLY)
    return None  # daily 不存在，允许生成


@pytest.fixture
def common_patches():
    """公共 mock 上下文"""
    with (
        patch("app.workers.schedule_worker.get_plan_for_period",
              new_callable=AsyncMock, side_effect=_mock_get_plan),
        patch("app.workers.schedule_worker.get_journal",
              new_callable=AsyncMock, return_value=MagicMock(content=FAKE_JOURNAL)),
        patch("app.workers.schedule_worker._get_persona_core",
              return_value=FAKE_PERSONA),
        patch("app.workers.schedule_worker._get_recent_daily_schedules",
              new_callable=AsyncMock, return_value=FAKE_RECENT),
        patch("app.workers.schedule_worker.upsert_schedule",
              new_callable=AsyncMock),
    ):
        yield


@pytest.mark.asyncio
async def test_pipeline_calls_ideation_writer_critic(common_patches):
    """管线按 Ideation → Writer → Critic 顺序执行"""
    call_order = []

    async def fake_ideation(**kw):
        call_order.append("ideation")
        return FAKE_IDEATION

    async def fake_writer(**kw):
        call_order.append("writer")
        return FAKE_SCHEDULE

    async def fake_critic(**kw):
        call_order.append("critic")
        return "PASS"

    with (
        patch("app.workers.schedule_worker._run_ideation",
              new_callable=AsyncMock, side_effect=fake_ideation),
        patch("app.workers.schedule_worker._run_writer",
              new_callable=AsyncMock, side_effect=fake_writer),
        patch("app.workers.schedule_worker._run_critic",
              new_callable=AsyncMock, side_effect=fake_critic),
    ):
        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(FAKE_DATE)

    assert call_order == ["ideation", "writer", "critic"]
    assert result == FAKE_SCHEDULE


@pytest.mark.asyncio
async def test_critic_reject_triggers_rewrite(common_patches):
    """Critic 不通过时 Writer 重写"""
    writer_call_count = 0

    async def fake_writer(**kw):
        nonlocal writer_call_count
        writer_call_count += 1
        return f"手帐v{writer_call_count}"

    critic_responses = iter(["建议：去掉雷同的胶片机意象", "PASS"])

    async def fake_critic(**kw):
        return next(critic_responses)

    with (
        patch("app.workers.schedule_worker._run_ideation",
              new_callable=AsyncMock, return_value=FAKE_IDEATION),
        patch("app.workers.schedule_worker._run_writer",
              new_callable=AsyncMock, side_effect=fake_writer),
        patch("app.workers.schedule_worker._run_critic",
              new_callable=AsyncMock, side_effect=fake_critic),
    ):
        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(FAKE_DATE)

    assert writer_call_count == 2
    assert result == "手帐v2"


@pytest.mark.asyncio
async def test_max_rewrite_attempts(common_patches):
    """3 轮都没 PASS → 用最后一版"""
    writer_call_count = 0

    async def fake_writer(**kw):
        nonlocal writer_call_count
        writer_call_count += 1
        return f"手帐v{writer_call_count}"

    async def fake_critic(**kw):
        return "建议：还是有问题"

    with (
        patch("app.workers.schedule_worker._run_ideation",
              new_callable=AsyncMock, return_value=FAKE_IDEATION),
        patch("app.workers.schedule_worker._run_writer",
              new_callable=AsyncMock, side_effect=fake_writer),
        patch("app.workers.schedule_worker._run_critic",
              new_callable=AsyncMock, side_effect=fake_critic),
    ):
        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(FAKE_DATE)

    assert writer_call_count == 3
    assert result == "手帐v3"


@pytest.mark.asyncio
async def test_ideation_failure_degrades_gracefully(common_patches):
    """Ideation 失败 → Writer 无素材降级"""
    async def failing_ideation(**kw):
        raise Exception("model timeout")

    writer_received_ideation = None

    async def capture_writer(**kw):
        nonlocal writer_received_ideation
        writer_received_ideation = kw.get("ideation_output", "NOT_FOUND")
        return FAKE_SCHEDULE

    async def fake_critic(**kw):
        return "PASS"

    with (
        patch("app.workers.schedule_worker._run_ideation",
              new_callable=AsyncMock, side_effect=failing_ideation),
        patch("app.workers.schedule_worker._run_writer",
              new_callable=AsyncMock, side_effect=capture_writer),
        patch("app.workers.schedule_worker._run_critic",
              new_callable=AsyncMock, side_effect=fake_critic),
    ):
        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(FAKE_DATE)

    assert result == FAKE_SCHEDULE
    assert writer_received_ideation == ""  # 降级为空字符串
```

- [ ] **Step 2: 运行测试确认全部 FAIL**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_schedule_pipeline.py -v`

Expected: 4 个测试全部 FAIL（`_run_ideation` / `_run_writer` / `_run_critic` / `_get_recent_daily_schedules` 不存在）

---

### Task 4: 实现 pipeline（绿）

**Files:**
- Modify: `apps/agent-service/app/workers/schedule_worker.py`

- [ ] **Step 1: 添加 `_get_recent_daily_schedules` 辅助函数**

在 `_get_persona_core()` 函数后面添加：

```python
async def _get_recent_daily_schedules(before_date: date, count: int = 3) -> list[AkaoSchedule]:
    """获取前 N 天的 daily schedule（供 Ideation 和 Critic 去重）"""
    results = await list_schedules(plan_type="daily", active_only=True, limit=count + 5)
    # 过滤掉 target_date 当天及之后的，取最近 count 条
    return [
        s for s in results
        if s.period_start < before_date.isoformat()
    ][:count]
```

需要在文件头 import 中添加 `list_schedules`：

```python
from app.orm.crud import (
    get_daily_entries_for_date,
    get_journal,
    get_latest_plan,
    get_plan_for_period,
    list_schedules,  # 新增
    upsert_schedule,
)
```

- [ ] **Step 2: 添加 `_run_ideation` 函数**

在辅助函数区域添加：

```python
async def _run_ideation(
    persona_core: str,
    yesterday_journal: str,
    recent_schedules_text: str,
    target_date: date,
) -> str:
    """运行 Ideation Agent：search_web tool-use loop 搜集生活素材"""
    from langchain.agents import create_agent
    from langchain.messages import HumanMessage
    from langfuse.langchain import CallbackHandler

    from app.agents.core.config import AgentRegistry
    from app.agents.tools.search.web import search_web

    config = AgentRegistry.get("schedule-ideation")
    prompt_template = get_prompt(config.prompt_id)

    season = _get_season(target_date.month)
    weekday = _WEEKDAY_CN[target_date.weekday()]

    compiled = prompt_template.compile(
        persona_core=persona_core,
        yesterday_journal=yesterday_journal,
        recent_schedules=recent_schedules_text,
        date=target_date.isoformat(),
        weekday=weekday,
        season=season,
    )

    model = await ModelBuilder.build_chat_model(config.model_id)
    agent = create_agent(model, [search_web], system_prompt=compiled)

    result = await agent.ainvoke(
        {"messages": [HumanMessage(content="开始搜集今天的生活素材吧。")]},
        config={
            "callbacks": [CallbackHandler()],
            "run_name": config.trace_name,
            "recursion_limit": 42,
        },
    )

    # 提取最后一条 AI message 的文本
    last_msg = result["messages"][-1]
    return _extract_text(last_msg.content)
```

- [ ] **Step 3: 添加 `_run_writer` 函数**

```python
async def _run_writer(
    ideation_output: str,
    persona_core: str,
    weekly_plan: str,
    yesterday_journal: str,
    target_date: date,
    previous_output: str = "",
    critic_feedback: str = "",
) -> str:
    """运行 Writer Agent：基于素材写手帐"""
    from app.agents.core.config import AgentRegistry

    config = AgentRegistry.get("schedule-writer")
    prompt_template = get_prompt(config.prompt_id)

    weekday = _WEEKDAY_CN[target_date.weekday()]
    is_weekend = "周末！" if target_date.weekday() >= 5 else ""

    compiled = prompt_template.compile(
        persona_core=persona_core,
        date=target_date.isoformat(),
        weekday=weekday,
        is_weekend=is_weekend,
        weekly_plan=weekly_plan,
        yesterday_journal=yesterday_journal,
        ideation_output=ideation_output,
        previous_output=previous_output,
        critic_feedback=critic_feedback,
    )

    model = await ModelBuilder.build_chat_model(config.model_id)
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    return _extract_text(response.content)
```

- [ ] **Step 4: 添加 `_run_critic` 函数**

```python
async def _run_critic(
    schedule_text: str,
    recent_schedules_text: str,
) -> str:
    """运行 Critic Agent：审查质量并返回 PASS 或修改建议"""
    from app.agents.core.config import AgentRegistry

    config = AgentRegistry.get("schedule-critic")
    prompt_template = get_prompt(config.prompt_id)

    compiled = prompt_template.compile(
        today_schedule=schedule_text,
        recent_schedules=recent_schedules_text,
    )

    model = await ModelBuilder.build_chat_model(config.model_id)
    response = await model.ainvoke([{"role": "user", "content": compiled}])
    return _extract_text(response.content)
```

- [ ] **Step 5: 重写 `generate_daily_plan()`**

删除 `generate_daily_plan()` 函数体中从 `# 3. 搜索多样化世界素材` 开始到 `return content` 的部分，替换为 pipeline 编排：

```python
async def generate_daily_plan(target_date: date | None = None) -> str | None:
    """生成日计划（手帐式 markdown）

    三 Agent 管线：Ideation（搜素材）→ Writer（写手帐）→ Critic（审查质量）
    Critic 不通过则 Writer 重写，最多 2 轮。

    Args:
        target_date: 目标日期，默认今天

    Returns:
        生成的手帐内容
    """
    if target_date is None:
        target_date = date.today()

    date_str = target_date.isoformat()

    # 检查是否已有
    existing = await get_plan_for_period("daily", date_str, date_str)
    if existing:
        logger.info(f"Daily plan already exists for {date_str}, skip")
        return existing.content

    # ---- 收集上下文 ----
    persona_core = _get_persona_core()

    # 周计划
    week_start = target_date - timedelta(days=target_date.weekday())
    week_end = week_start + timedelta(days=6)
    weekly = await get_plan_for_period("weekly", week_start.isoformat(), week_end.isoformat())
    weekly_text = weekly.content if weekly else "（暂无周计划）"

    # 昨天 Journal
    yesterday = (target_date - timedelta(days=1)).isoformat()
    yesterday_journal_entry = await get_journal("daily", yesterday)
    yesterday_journal = yesterday_journal_entry.content if yesterday_journal_entry else "（昨天没有写日志）"

    # 前 3 天 schedule（Ideation 和 Critic 共用）
    recent = await _get_recent_daily_schedules(target_date)
    recent_schedules_text = "\n\n---\n\n".join(
        f"[{s.period_start}]\n{s.content}" for s in recent
    ) if recent else "（没有前几天的日程）"

    # ---- Ideation Agent ----
    try:
        ideation_output = await _run_ideation(
            persona_core=persona_core,
            yesterday_journal=yesterday_journal,
            recent_schedules_text=recent_schedules_text,
            target_date=target_date,
        )
    except Exception as e:
        logger.warning(f"Ideation agent failed, degrading: {e}", exc_info=True)
        ideation_output = ""

    # ---- Writer → Critic 循环 ----
    feedback = ""
    previous_output = ""
    schedule_text = ""

    for attempt in range(3):
        schedule_text = await _run_writer(
            ideation_output=ideation_output,
            persona_core=persona_core,
            weekly_plan=weekly_text,
            yesterday_journal=yesterday_journal,
            target_date=target_date,
            previous_output=previous_output,
            critic_feedback=feedback,
        )

        critic_result = await _run_critic(
            schedule_text=schedule_text,
            recent_schedules_text=recent_schedules_text,
        )

        if "PASS" in critic_result:
            logger.info(f"Daily plan passed critic on attempt {attempt + 1}")
            break

        logger.info(f"Daily plan critic rejected (attempt {attempt + 1}): {critic_result[:100]}")
        previous_output = schedule_text
        feedback = critic_result

    if not schedule_text:
        logger.warning(f"Pipeline produced empty daily plan for {date_str}")
        return None

    # ---- 存储 ----
    await upsert_schedule(AkaoSchedule(
        plan_type="daily",
        period_start=date_str,
        period_end=date_str,
        content=schedule_text,
        model="offline-model",
    ))

    logger.info(f"Daily plan generated for {date_str}: {len(schedule_text)} chars")
    return schedule_text
```

- [ ] **Step 6: 删除旧代码**

删除以下函数和常量（约 115 行）：
- `_WORLD_CONTEXT_DIMENSIONS`（第 67-131 行）
- `_select_dimensions()`（第 134-148 行）
- `_build_active_dimensions_text()`（第 151-154 行）
- `_gather_world_context()`（第 157-192 行）

同时删除不再需要的 import：`random`

- [ ] **Step 7: 运行 pipeline 测试确认全部 PASS**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_schedule_pipeline.py -v`

Expected: 4 个测试全部 PASS

- [ ] **Step 8: Commit**

```bash
git add apps/agent-service/app/workers/schedule_worker.py
git commit -m "feat(schedule): replace single LLM call with Ideation→Writer→Critic pipeline"
```

---

### Task 5: 清理旧测试 + 全量验证

**Files:**
- Delete: `apps/agent-service/tests/unit/test_schedule_dimensions.py`

- [ ] **Step 1: 删除旧的 dimension 测试文件**

删除 `apps/agent-service/tests/unit/test_schedule_dimensions.py`，这些测试覆盖的 `_select_dimensions`、`_build_active_dimensions_text` 和基于旧 `_gather_world_context` 的 `test_generate_daily_plan_uses_journal` 已被 Task 3 的 pipeline 测试替代。

- [ ] **Step 2: 运行全量测试**

Run: `cd apps/agent-service && uv run pytest tests/ -v --timeout=30`

Expected: 所有测试 PASS，无报错

- [ ] **Step 3: Commit**

```bash
git add -A
git commit -m "test(schedule): replace dimension tests with pipeline tests"
```

---

### Task 6: 部署到 context-v3 泳道验证

- [ ] **Step 1: Push 分支**

```bash
git push
```

- [ ] **Step 2: 部署**

```bash
make deploy APP=agent-service LANE=context-v3 GIT_REF=docs/review-context-system
```

- [ ] **Step 3: 手动触发 daily plan 生成**

通过 API 触发一次 `generate_daily_plan()`（backfill 模式），检查 Langfuse traces 确认三 Agent 依次执行、search_web 被 Ideation 自主调用、Critic 审查输出。

- [ ] **Step 4: 对比效果**

用 `/ops-db` 读取新生成的 schedule，与旧的 3 天 schedule 对比：
- 主题是否不再趋同
- 是否有来自搜索的真实素材
- 是否通过了 Critic 审查
