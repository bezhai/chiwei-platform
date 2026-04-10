# agent-service 架构重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 消除 agent-service 中的重复代码和职责混乱，建立可复用的抽象层

**Architecture:** 按依赖层级分 4 阶段推进（基础设施 → 管线 → 数据 → 编排），每个 commit 闭环（测试 → 实现 → 迁移 → 删旧代码）

**Tech Stack:** Python 3.10+, FastAPI, LangChain/LangGraph, SQLAlchemy async, pytest + pytest-asyncio

**设计文档:** `docs/superpowers/specs/2026-04-10-agent-service-architecture-refactor.md`

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `app/agents/infra/llm_service.py` | 统一 LLM 调用入口（run/stream/extract） |
| `app/services/persona_loader.py` | 统一 Persona 上下文加载（带缓存） |
| `app/services/timeline_formatter.py` | 统一消息时间线格式化 |
| `app/services/debounced_pipeline.py` | 两阶段防抖管线基类 |
| `app/agents/tools/image/processor.py` | 统一图片上传注册 |
| `app/orm/crud/__init__.py` | CRUD re-export 兼容层 |
| `app/orm/crud/persona.py` | Persona 相关 CRUD |
| `app/orm/crud/model_provider.py` | 模型配置 CRUD |
| `app/orm/crud/message.py` | 消息相关 CRUD |
| `app/orm/crud/schedule.py` | 手帐相关 CRUD |
| `app/orm/crud/life_engine.py` | Life Engine 状态 CRUD |
| `app/agents/domains/main/safety_race.py` | Pre-safety 竞速逻辑 |
| `app/agents/domains/main/stream_handler.py` | 流式输出处理 |
| `app/agents/domains/main/post_actions.py` | 后处理 fire-and-forget 任务 |
| `app/agents/graphs/shared/banned_word.py` | 共享 banned_word 检查 |
| `app/workers/error_handling.py` | Worker 统一错误处理装饰器 |

### New Test Files

| File | Tests |
|------|-------|
| `tests/unit/test_llm_service.py` | LLMService.run / extract / stream |
| `tests/unit/test_persona_loader.py` | PersonaLoader 加载 + 缓存 |
| `tests/unit/test_timeline_formatter.py` | TimelineFormatter 格式化 |
| `tests/unit/test_debounced_pipeline.py` | DebouncedPipeline 防抖 + flush |
| `tests/unit/test_image_processor.py` | ImageProcessor 上传注册 |
| `tests/unit/test_crud_persona.py` | Persona CRUD 函数 |
| `tests/unit/test_crud_message.py` | Message CRUD 函数 |
| `tests/unit/test_crud_life_engine.py` | Life Engine CRUD 函数 |
| `tests/unit/test_safety_race.py` | Pre-safety 竞速逻辑 |
| `tests/unit/test_stream_handler.py` | 流式输出处理 |
| `tests/unit/test_worker_error_handling.py` | Worker 错误处理装饰器 |

### Files to Delete (after migration)

旧的 `app/orm/crud.py` 在 Phase 3 被拆分为 `app/orm/crud/` 目录，原文件删除。

---

## Phase 1: 基础设施层

### Task 1: 创建 LLMService

**Files:**
- Create: `app/agents/infra/llm_service.py`
- Create: `tests/unit/test_llm_service.py`
- Reference: `app/agents/core/agent.py` (ChatAgent implementation)
- Reference: `app/agents/infra/model_builder.py` (ModelBuilder)
- Reference: `app/agents/infra/langfuse_client.py` (get_prompt, get_client)

- [ ] **Step 1: Write failing tests for LLMService.run**

```python
# tests/unit/test_llm_service.py
"""LLMService 统一 LLM 调用入口的单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from langchain_core.messages import AIMessage, HumanMessage


class TestLLMServiceRun:
    """LLMService.run — 非流式调用"""

    @pytest.mark.asyncio
    async def test_run_returns_ai_message(self):
        """正常调用返回 AIMessage"""
        expected = AIMessage(content="你好呀")
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=expected)

        mock_prompt = MagicMock()
        mock_prompt.compile.return_value = "compiled system prompt"

        with (
            patch(
                "app.agents.infra.llm_service.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_model,
            ),
            patch(
                "app.agents.infra.llm_service.get_prompt",
                return_value=mock_prompt,
            ),
            patch(
                "app.agents.infra.llm_service.CallbackHandler",
                return_value=MagicMock(),
            ),
        ):
            from app.agents.infra.llm_service import LLMService

            result = await LLMService.run(
                prompt_id="test_prompt",
                prompt_vars={"name": "赤尾"},
                messages=[HumanMessage(content="你好")],
                model_id="test-model",
                trace_name="test-trace",
            )

        assert result == expected
        mock_prompt.compile.assert_called_once_with(name="赤尾")
        mock_model.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_passes_callbacks_to_model(self):
        """确保 Langfuse CallbackHandler 传入 model config"""
        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(return_value=AIMessage(content="ok"))
        mock_cb = MagicMock()

        with (
            patch(
                "app.agents.infra.llm_service.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_model,
            ),
            patch(
                "app.agents.infra.llm_service.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value="sys")),
            ),
            patch(
                "app.agents.infra.llm_service.CallbackHandler",
                return_value=mock_cb,
            ),
        ):
            from app.agents.infra.llm_service import LLMService

            await LLMService.run(
                prompt_id="p",
                prompt_vars={},
                messages=[],
                trace_name="my-trace",
            )

        # 验证 config 中包含 callbacks 和 run_name
        call_kwargs = mock_model.ainvoke.call_args
        config = call_kwargs.kwargs.get("config") or call_kwargs[1].get("config")
        assert mock_cb in config["callbacks"]
        assert config["run_name"] == "my-trace"

    @pytest.mark.asyncio
    async def test_run_retries_on_transient_error(self):
        """遇到临时错误时重试"""
        from openai import APITimeoutError

        mock_model = MagicMock()
        mock_model.ainvoke = AsyncMock(
            side_effect=[
                APITimeoutError(request=MagicMock()),
                AIMessage(content="ok"),
            ]
        )

        with (
            patch(
                "app.agents.infra.llm_service.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_model,
            ),
            patch(
                "app.agents.infra.llm_service.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value="sys")),
            ),
            patch(
                "app.agents.infra.llm_service.CallbackHandler",
                return_value=MagicMock(),
            ),
        ):
            from app.agents.infra.llm_service import LLMService

            result = await LLMService.run(
                prompt_id="p",
                prompt_vars={},
                messages=[],
                max_retries=2,
            )

        assert result.content == "ok"
        assert mock_model.ainvoke.await_count == 2


class TestLLMServiceExtract:
    """LLMService.extract — 结构化输出"""

    @pytest.mark.asyncio
    async def test_extract_returns_pydantic_model(self):
        """extract 返回 Pydantic model 实例"""
        from pydantic import BaseModel

        class SafetyResult(BaseModel):
            is_safe: bool
            confidence: float

        expected = SafetyResult(is_safe=True, confidence=0.95)
        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(return_value=expected)
        mock_model = MagicMock()
        mock_model.with_structured_output.return_value = mock_structured

        with (
            patch(
                "app.agents.infra.llm_service.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_model,
            ),
            patch(
                "app.agents.infra.llm_service.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value="sys")),
            ),
            patch(
                "app.agents.infra.llm_service.CallbackHandler",
                return_value=MagicMock(),
            ),
        ):
            from app.agents.infra.llm_service import LLMService

            result = await LLMService.extract(
                prompt_id="guard_prompt",
                prompt_vars={},
                messages=[HumanMessage(content="test")],
                schema=SafetyResult,
                model_id="guard-model",
            )

        assert isinstance(result, SafetyResult)
        assert result.is_safe is True
        mock_model.with_structured_output.assert_called_once_with(SafetyResult)

    @pytest.mark.asyncio
    async def test_extract_passes_model_kwargs(self):
        """extract 将额外参数（如 reasoning_effort）传给 ModelBuilder"""
        from pydantic import BaseModel

        class Dummy(BaseModel):
            x: int

        mock_structured = MagicMock()
        mock_structured.ainvoke = AsyncMock(return_value=Dummy(x=1))
        mock_model = MagicMock()
        mock_model.with_structured_output.return_value = mock_structured

        with (
            patch(
                "app.agents.infra.llm_service.ModelBuilder.build_chat_model",
                new_callable=AsyncMock,
                return_value=mock_model,
            ) as mock_build,
            patch(
                "app.agents.infra.llm_service.get_prompt",
                return_value=MagicMock(compile=MagicMock(return_value="sys")),
            ),
            patch(
                "app.agents.infra.llm_service.CallbackHandler",
                return_value=MagicMock(),
            ),
        ):
            from app.agents.infra.llm_service import LLMService

            await LLMService.extract(
                prompt_id="p",
                prompt_vars={},
                messages=[],
                schema=Dummy,
                model_id="guard-model",
                model_kwargs={"reasoning_effort": "low"},
            )

        mock_build.assert_awaited_once_with("guard-model", reasoning_effort="low")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_llm_service.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agents.infra.llm_service'`

- [ ] **Step 3: Implement LLMService**

```python
# app/agents/infra/llm_service.py
"""统一 LLM 调用入口。

所有 LLM 调用都通过 LLMService，自带 Langfuse trace + 重试。
禁止在 services/workers 中直接使用 ModelBuilder.build_chat_model + model.ainvoke。
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, AsyncGenerator

from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, SystemMessage
from langfuse.callback import CallbackHandler

from app.agents.infra.langfuse_client import get_prompt
from app.agents.infra.model_builder import ModelBuilder

if TYPE_CHECKING:
    from pydantic import BaseModel

logger = logging.getLogger(__name__)

# 可重试的异常类型
_RETRYABLE_EXCEPTIONS: tuple[type[Exception], ...] = ()
try:
    from openai import APIConnectionError, APITimeoutError, RateLimitError

    _RETRYABLE_EXCEPTIONS = (APITimeoutError, APIConnectionError, RateLimitError)
except ImportError:
    pass


def _build_callbacks(
    trace_name: str,
    parent_run_id: str | None = None,
    metadata: dict | None = None,
) -> list:
    """构建 Langfuse CallbackHandler 列表"""
    kwargs: dict = {}
    if parent_run_id:
        kwargs["trace_id"] = parent_run_id
    if metadata:
        kwargs["metadata"] = metadata
    return [CallbackHandler(trace_name=trace_name, **kwargs)]


def _build_messages(
    system_prompt: str,
    messages: list[BaseMessage],
) -> list[BaseMessage]:
    """system prompt + 用户消息"""
    return [SystemMessage(content=system_prompt), *messages]


class LLMService:
    """统一 LLM 调用入口。"""

    @staticmethod
    async def run(
        prompt_id: str,
        prompt_vars: dict,
        messages: list[BaseMessage],
        *,
        model_id: str | None = None,
        trace_name: str | None = None,
        parent_run_id: str | None = None,
        metadata: dict | None = None,
        max_retries: int = 3,
        model_kwargs: dict | None = None,
    ) -> AIMessage:
        """非流式 LLM 调用"""
        _trace = trace_name or prompt_id
        model = await ModelBuilder.build_chat_model(
            model_id or prompt_id, **(model_kwargs or {})
        )
        prompt = get_prompt(prompt_id)
        system_prompt = prompt.compile(**prompt_vars)
        full_messages = _build_messages(system_prompt, messages)
        callbacks = _build_callbacks(_trace, parent_run_id, metadata)
        config = {"callbacks": callbacks, "run_name": _trace}

        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                return await model.ainvoke(full_messages, config=config)
            except _RETRYABLE_EXCEPTIONS as e:
                last_err = e
                wait = min(2**attempt, 8)
                logger.warning(
                    "LLMService.run retry %d/%d for %s: %s",
                    attempt + 1,
                    max_retries,
                    _trace,
                    e,
                )
                await asyncio.sleep(wait)
        raise last_err  # type: ignore[misc]

    @staticmethod
    async def stream(
        prompt_id: str,
        prompt_vars: dict,
        messages: list[BaseMessage],
        *,
        model_id: str | None = None,
        trace_name: str | None = None,
        parent_run_id: str | None = None,
        metadata: dict | None = None,
        model_kwargs: dict | None = None,
    ) -> AsyncGenerator[AIMessageChunk, None]:
        """流式 LLM 调用"""
        _trace = trace_name or prompt_id
        model = await ModelBuilder.build_chat_model(
            model_id or prompt_id, **(model_kwargs or {})
        )
        prompt = get_prompt(prompt_id)
        system_prompt = prompt.compile(**prompt_vars)
        full_messages = _build_messages(system_prompt, messages)
        callbacks = _build_callbacks(_trace, parent_run_id, metadata)
        config = {"callbacks": callbacks, "run_name": _trace}

        async for chunk in model.astream(full_messages, config=config):
            yield chunk

    @staticmethod
    async def extract(
        prompt_id: str,
        prompt_vars: dict,
        messages: list[BaseMessage],
        schema: type[BaseModel],
        *,
        model_id: str | None = None,
        trace_name: str | None = None,
        parent_run_id: str | None = None,
        max_retries: int = 3,
        model_kwargs: dict | None = None,
    ) -> BaseModel:
        """结构化输出 LLM 调用"""
        _trace = trace_name or prompt_id
        model = await ModelBuilder.build_chat_model(
            model_id or prompt_id, **(model_kwargs or {})
        )
        structured_model = model.with_structured_output(schema)
        prompt = get_prompt(prompt_id)
        system_prompt = prompt.compile(**prompt_vars)
        full_messages = _build_messages(system_prompt, messages)
        callbacks = _build_callbacks(_trace, parent_run_id)
        config = {"callbacks": callbacks, "run_name": _trace}

        last_err: Exception | None = None
        for attempt in range(max_retries):
            try:
                return await structured_model.ainvoke(full_messages, config=config)
            except _RETRYABLE_EXCEPTIONS as e:
                last_err = e
                wait = min(2**attempt, 8)
                logger.warning(
                    "LLMService.extract retry %d/%d for %s: %s",
                    attempt + 1,
                    max_retries,
                    _trace,
                    e,
                )
                await asyncio.sleep(wait)
        raise last_err  # type: ignore[misc]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_llm_service.py -v`
Expected: All PASS

- [ ] **Step 5: Run full test suite to verify no regressions**

Run: `cd apps/agent-service && uv run pytest -x -q`
Expected: All existing tests still pass

- [ ] **Step 6: Commit**

```bash
git add app/agents/infra/llm_service.py tests/unit/test_llm_service.py
git commit -m "feat(agent-service): add LLMService — unified LLM invocation entry point

Provides run/stream/extract methods with automatic Langfuse tracing,
retry on transient errors, and consistent prompt compilation.
Next step: migrate all direct ModelBuilder/ChatAgent callers."
```

---

### Task 2: 迁移所有调用方到 LLMService

**Files:**
- Modify: `app/services/life_engine.py` (lines ~163-212: 替换 ModelBuilder 直接调用)
- Modify: `app/services/glimpse.py` (lines ~110-160: 替换 ModelBuilder 直接调用)
- Modify: `app/services/afterthought.py` (lines ~166-180: 替换 ChatAgent 调用)
- Modify: `app/services/voice_generator.py` (lines ~53-71: 替换 ChatAgent 调用)
- Modify: `app/services/relationship_memory.py` (lines ~95-108, 191-206: 替换 ChatAgent 调用)
- Modify: `app/agents/graphs/pre/nodes/safety.py` (lines ~69-76, 105-112: 替换裸 structured_output)
- Modify: `app/agents/graphs/pre/nodes/complexity.py` (lines ~43-50: 替换裸 structured_output)
- Modify: `app/agents/graphs/pre/nodes/nsfw_safety.py` (lines ~41-48: 替换裸 structured_output)
- Modify: `app/agents/graphs/post/safety.py` (lines ~61-71: 替换裸 structured_output)
- Modify: `app/workers/dream_worker.py` (替换 ChatAgent 调用)
- Modify: `app/workers/schedule_worker.py` (替换 ChatAgent + ModelBuilder 混用)
- Test: existing tests in `tests/`

每个文件的迁移模式相同：

**Pattern A: ModelBuilder 直接调用 → LLMService.run**

Before (life_engine.py 示例):
```python
from app.agents.infra.model_builder import ModelBuilder
from app.agents.infra.langfuse_client import get_prompt
from langfuse.callback import CallbackHandler

model = await ModelBuilder.build_chat_model(settings.life_engine_model)
prompt = get_prompt("life_engine_tick")
compiled = prompt.compile(**vars)
response = await model.ainvoke(
    [{"role": "user", "content": compiled}],
    config={"callbacks": [CallbackHandler()], "run_name": "life-engine-tick"},
)
```

After:
```python
from app.agents.infra.llm_service import LLMService

response = await LLMService.run(
    prompt_id="life_engine_tick",
    prompt_vars=vars,
    messages=[HumanMessage(content=user_content)],
    model_id=settings.life_engine_model,
    trace_name="life-engine-tick",
)
```

**Pattern B: ChatAgent 调用 → LLMService.run**

Before (afterthought.py 示例):
```python
from app.agents.core.agent import ChatAgent

agent = ChatAgent(
    prompt_id="afterthought_conversation",
    tools=[],
    model_id=settings.diary_model,
    trace_name="afterthought",
)
result = await agent.run(
    messages=[HumanMessage(content=timeline)],
    prompt_vars={"persona_name": name, "persona_lite": lite, "scene": scene},
)
```

After:
```python
from app.agents.infra.llm_service import LLMService

result = await LLMService.run(
    prompt_id="afterthought_conversation",
    prompt_vars={"persona_name": name, "persona_lite": lite, "scene": scene},
    messages=[HumanMessage(content=timeline)],
    model_id=settings.diary_model,
    trace_name="afterthought",
)
```

**Pattern C: 裸 structured_output → LLMService.extract**

Before (safety.py 示例):
```python
from app.agents.infra.model_builder import ModelBuilder

model = await ModelBuilder.build_chat_model("guard-model", reasoning_effort="low")
structured_model = model.with_structured_output(PromptInjectionResult)
result = await structured_model.ainvoke(messages, config=config)
```

After:
```python
from app.agents.infra.llm_service import LLMService

result = await LLMService.extract(
    prompt_id="guard_injection",
    prompt_vars={},
    messages=[HumanMessage(content=user_message)],
    schema=PromptInjectionResult,
    model_id="guard-model",
    trace_name="pre-injection-check",
    model_kwargs={"reasoning_effort": "low"},
)
```

注意：safety 节点当前直接传 compiled prompt 作为 messages，不走 get_prompt。需要确认它们是否有对应的 Langfuse prompt，如果没有则保留现有 prompt 编译方式，LLMService 需要支持不走 get_prompt 的模式（传 `prompt_id=None` + 直接传 messages 含 SystemMessage）。

在实现迁移前，先读每个文件确认当前的精确调用方式，再做替换。

- [ ] **Step 1: 读取所有待迁移文件，确认当前调用方式**

逐个读取上述文件，记录每个 LLM 调用的：
- 精确行号
- prompt 来源（get_prompt vs 内联）
- model_id 来源
- 是否有 CallbackHandler/trace

- [ ] **Step 2: 评估 LLMService 接口是否需要调整**

检查是否有调用方的模式不匹配 LLMService 当前接口，例如：
- safety 节点是否使用 get_prompt 还是内联 prompt
- schedule_worker 是否需要 stream
- 是否有调用方传 tools（主 agent 的 stream_chat 暂不迁移，留在 Phase 4）

如果需要，先调整 LLMService 接口（如添加 `system_prompt` 直传参数），更新测试，再继续迁移。

- [ ] **Step 3: 逐个迁移，每个文件迁移后运行该文件的现有测试**

迁移顺序（从简到繁）：
1. `voice_generator.py` → 运行 `pytest tests/unit/test_voice_generator.py -v`（如果存在）
2. `afterthought.py` → 运行 `pytest tests/unit/test_afterthought.py -v`
3. `relationship_memory.py` → 运行相关测试
4. `life_engine.py` → 运行 `pytest tests/unit/test_life_engine.py -v`
5. `glimpse.py` → 运行 `pytest tests/unit/test_glimpse.py -v`
6. `dream_worker.py` → 运行 `pytest tests/unit/test_dream_worker.py -v`
7. `schedule_worker.py` → 运行 `pytest tests/unit/test_schedule_pipeline.py -v`
8. `safety.py` (pre) → 运行 `pytest tests/unit/test_pre_state.py tests/integration/test_pre_graph.py -v`
9. `complexity.py` → 同上
10. `nsfw_safety.py` → 运行 `pytest tests/unit/test_nsfw_safety.py -v`
11. `post/safety.py` → 运行 `pytest tests/unit/test_post_safety.py -v`

每个文件需要更新对应测试中的 mock 路径（从 `app.agents.infra.model_builder.ModelBuilder.build_chat_model` 改为 `app.agents.infra.llm_service.LLMService.run` 等）。

- [ ] **Step 4: 运行全量测试**

Run: `cd apps/agent-service && uv run pytest -x -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "refactor(agent-service): migrate all LLM callers to LLMService

Unified 4 different invocation patterns (ChatAgent, ModelBuilder+ainvoke,
raw structured_output, embedding) into LLMService.run/extract.
All callers now have consistent Langfuse tracing and retry."
```

---

### Task 3: 创建 PersonaLoader + 迁移调用方

**Files:**
- Create: `app/services/persona_loader.py`
- Create: `tests/unit/test_persona_loader.py`
- Modify: `app/services/identity_drift.py` (删除 `_get_persona_context`)
- Modify: `app/services/glimpse.py` (删除 `_get_persona_info`)
- Modify: `app/services/afterthought.py` (删除内联 persona 加载)
- Modify: `app/services/voice_generator.py` (删除内联 persona 加载)
- Modify: `app/services/relationship_memory.py` (删除内联 persona 加载)

- [ ] **Step 1: Write failing tests for PersonaLoader**

```python
# tests/unit/test_persona_loader.py
"""PersonaLoader 统一 Persona 上下文加载的单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestLoadPersona:

    @pytest.mark.asyncio
    async def test_load_persona_returns_context(self):
        """正常加载返回 PersonaContext"""
        mock_persona = MagicMock()
        mock_persona.display_name = "赤尾"
        mock_persona.persona_lite = "元气满满的少女"
        mock_persona.bot_name = "akao-bot"

        with patch(
            "app.services.persona_loader.get_bot_persona",
            new_callable=AsyncMock,
            return_value=mock_persona,
        ):
            from app.services.persona_loader import load_persona

            result = await load_persona("akao")

        assert result.persona_id == "akao"
        assert result.display_name == "赤尾"
        assert result.persona_lite == "元气满满的少女"
        assert result.bot_name == "akao-bot"

    @pytest.mark.asyncio
    async def test_load_persona_fallback_when_not_found(self):
        """persona 不存在时返回 fallback"""
        with patch(
            "app.services.persona_loader.get_bot_persona",
            new_callable=AsyncMock,
            return_value=None,
        ):
            from app.services.persona_loader import load_persona

            result = await load_persona("unknown")

        assert result.persona_id == "unknown"
        assert result.display_name == "unknown"
        assert result.persona_lite == ""

    @pytest.mark.asyncio
    async def test_load_persona_caches_result(self):
        """相同 persona_id 第二次调用走缓存"""
        mock_persona = MagicMock()
        mock_persona.display_name = "赤尾"
        mock_persona.persona_lite = "lite"
        mock_persona.bot_name = None

        with patch(
            "app.services.persona_loader.get_bot_persona",
            new_callable=AsyncMock,
            return_value=mock_persona,
        ) as mock_get:
            from app.services.persona_loader import load_persona, _persona_cache

            _persona_cache.clear()
            await load_persona("akao")
            await load_persona("akao")

        # 只调用了一次 DB
        mock_get.assert_awaited_once_with("akao")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_persona_loader.py -v`
Expected: FAIL

- [ ] **Step 3: Implement PersonaLoader**

```python
# app/services/persona_loader.py
"""统一 Persona 上下文加载。

所有需要 persona display_name / persona_lite 的地方统一调用 load_persona()。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.orm.crud import get_bot_persona

_CACHE_TTL = 300  # 5 minutes

@dataclass(frozen=True)
class PersonaContext:
    persona_id: str
    display_name: str
    persona_lite: str
    bot_name: str | None = None


# 简单的 TTL 缓存：{persona_id: (PersonaContext, expire_time)}
_persona_cache: dict[str, tuple[PersonaContext, float]] = {}


async def load_persona(persona_id: str) -> PersonaContext:
    """加载 persona 上下文，带进程内 TTL 缓存"""
    now = time.monotonic()
    cached = _persona_cache.get(persona_id)
    if cached and cached[1] > now:
        return cached[0]

    persona = await get_bot_persona(persona_id)
    if persona:
        ctx = PersonaContext(
            persona_id=persona_id,
            display_name=persona.display_name,
            persona_lite=persona.persona_lite or "",
            bot_name=getattr(persona, "bot_name", None),
        )
    else:
        ctx = PersonaContext(
            persona_id=persona_id,
            display_name=persona_id,
            persona_lite="",
        )

    _persona_cache[persona_id] = (ctx, now + _CACHE_TTL)
    return ctx
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_persona_loader.py -v`
Expected: All PASS

- [ ] **Step 5: 迁移 5 个调用方**

每个文件：删除内联的 persona 加载逻辑，替换为 `from app.services.persona_loader import load_persona`。

示例 (identity_drift.py):

Before:
```python
async def _get_persona_context(persona_id: str) -> tuple[str, str]:
    from app.orm.crud import get_bot_persona
    persona = await get_bot_persona(persona_id)
    if persona:
        return persona.display_name, persona.persona_lite
    return persona_id, ""
```

After:
```python
from app.services.persona_loader import load_persona

# 在使用处:
pc = await load_persona(persona_id)
# 用 pc.display_name, pc.persona_lite 替代原来的 tuple 解包
```

对 5 个文件逐个替换，每个替换后运行该文件对应的测试。

- [ ] **Step 6: Run full test suite**

Run: `cd apps/agent-service && uv run pytest -x -q`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add app/services/persona_loader.py tests/unit/test_persona_loader.py -u
git commit -m "refactor(agent-service): add PersonaLoader, deduplicate 5 inline persona loading patterns

Cached persona context loading replaces identical get_bot_persona + tuple
extraction in identity_drift, glimpse, afterthought, voice_generator,
and relationship_memory."
```

---

## Phase 2: 管线层

### Task 4: 创建 TimelineFormatter + 迁移调用方

**Files:**
- Create: `app/services/timeline_formatter.py`
- Create: `tests/unit/test_timeline_formatter.py`
- Modify: `app/services/relationship_memory.py` (删除 `format_timeline`)
- Modify: `app/services/glimpse.py` (删除 `_format_messages`)
- Modify: `app/services/identity_drift.py` (删除 `_get_recent_messages`)
- Modify: `app/services/afterthought.py` (import 改指新模块)

- [ ] **Step 1: Write failing tests for TimelineFormatter**

```python
# tests/unit/test_timeline_formatter.py
"""TimelineFormatter 统一消息时间线格式化的单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import timezone


def _make_msg(role, content, user_id="u1", create_time=1712700000000, msg_id=None):
    """构造 mock ConversationMessage"""
    msg = MagicMock()
    msg.role = role
    msg.content = content
    msg.user_id = user_id
    msg.create_time = create_time
    msg.id = msg_id
    return msg


class TestFormatTimeline:

    @pytest.mark.asyncio
    async def test_basic_formatting(self):
        """基本格式：[HH:MM] speaker: content"""
        messages = [
            _make_msg("user", "你好", user_id="user1", create_time=1712700000000),
            _make_msg("assistant", "你好呀", create_time=1712700060000),
        ]

        with patch(
            "app.services.timeline_formatter.get_username",
            new_callable=AsyncMock,
            return_value="小明",
        ), patch(
            "app.services.timeline_formatter.parse_content",
            side_effect=lambda c: MagicMock(render=MagicMock(return_value=c)),
        ):
            from app.services.timeline_formatter import format_timeline

            result = await format_timeline(
                messages=messages,
                persona_name="赤尾",
                tz=timezone.utc,
            )

        lines = result.strip().split("\n")
        assert len(lines) == 2
        assert "小明" in lines[0]
        assert "赤尾" in lines[1]

    @pytest.mark.asyncio
    async def test_with_ids(self):
        """with_ids=True 时消息前有 #id"""
        messages = [
            _make_msg("user", "hello", msg_id="msg-123", create_time=1712700000000),
        ]

        with patch(
            "app.services.timeline_formatter.get_username",
            new_callable=AsyncMock,
            return_value="小明",
        ), patch(
            "app.services.timeline_formatter.parse_content",
            side_effect=lambda c: MagicMock(render=MagicMock(return_value=c)),
        ):
            from app.services.timeline_formatter import format_timeline

            result = await format_timeline(
                messages=messages,
                persona_name="赤尾",
                with_ids=True,
                tz=timezone.utc,
            )

        assert "#msg-123" in result

    @pytest.mark.asyncio
    async def test_max_messages_truncation(self):
        """max_messages 截断"""
        messages = [
            _make_msg("user", f"msg{i}", create_time=1712700000000 + i * 1000)
            for i in range(10)
        ]

        with patch(
            "app.services.timeline_formatter.get_username",
            new_callable=AsyncMock,
            return_value="小明",
        ), patch(
            "app.services.timeline_formatter.parse_content",
            side_effect=lambda c: MagicMock(render=MagicMock(return_value=c)),
        ):
            from app.services.timeline_formatter import format_timeline

            result = await format_timeline(
                messages=messages,
                persona_name="赤尾",
                max_messages=3,
                tz=timezone.utc,
            )

        lines = result.strip().split("\n")
        assert len(lines) == 3

    @pytest.mark.asyncio
    async def test_empty_content_skipped(self):
        """空消息内容被跳过"""
        messages = [
            _make_msg("user", "", create_time=1712700000000),
            _make_msg("user", "  ", create_time=1712700060000),
            _make_msg("user", "hello", create_time=1712700120000),
        ]

        with patch(
            "app.services.timeline_formatter.get_username",
            new_callable=AsyncMock,
            return_value="小明",
        ), patch(
            "app.services.timeline_formatter.parse_content",
            side_effect=lambda c: MagicMock(render=MagicMock(return_value=c)),
        ):
            from app.services.timeline_formatter import format_timeline

            result = await format_timeline(
                messages=messages,
                persona_name="赤尾",
                tz=timezone.utc,
            )

        lines = result.strip().split("\n")
        assert len(lines) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_timeline_formatter.py -v`
Expected: FAIL

- [ ] **Step 3: Implement TimelineFormatter**

```python
# app/services/timeline_formatter.py
"""统一消息时间线格式化。

将消息列表格式化为 [HH:MM] speaker: content 的文本。
所有需要将消息格式化为 prompt 变量的场景统一使用此函数。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from app.orm.crud import get_username
from app.utils.content_parser import parse_content


async def format_timeline(
    messages: list,
    persona_name: str,
    *,
    tz: timezone | ZoneInfo | None = None,
    max_messages: int | None = None,
    with_ids: bool = False,
    username_resolver: Callable | None = None,
) -> str:
    """格式化消息列表为时间线文本

    格式: [HH:MM] speaker: content（截断 200 字）
    with_ids=True 时: #id [HH:MM] speaker: content
    """
    _tz = tz or timezone.utc
    _resolve = username_resolver or get_username

    if max_messages:
        messages = messages[-max_messages:]

    lines: list[str] = []
    for msg in messages:
        msg_time = datetime.fromtimestamp(msg.create_time / 1000, tz=_tz)
        time_str = msg_time.strftime("%H:%M")

        if msg.role == "assistant":
            speaker = persona_name
        else:
            name = await _resolve(msg.user_id)
            speaker = name or msg.user_id[:6]

        rendered = parse_content(msg.content).render()
        if not rendered or not rendered.strip():
            continue

        prefix = f"#{msg.id} " if with_ids and msg.id else ""
        lines.append(f"{prefix}[{time_str}] {speaker}: {rendered[:200]}")

    return "\n".join(lines)
```

- [ ] **Step 4: Run tests, then migrate 4 callers, then run full suite**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_timeline_formatter.py -v`

迁移 relationship_memory.py、glimpse.py、identity_drift.py、afterthought.py，删除各自的格式化函数。

Run: `cd apps/agent-service && uv run pytest -x -q`

- [ ] **Step 5: Commit**

```bash
git add app/services/timeline_formatter.py tests/unit/test_timeline_formatter.py -u
git commit -m "refactor(agent-service): add TimelineFormatter, deduplicate 4 timeline formatting implementations

Unified format_timeline replaces relationship_memory.format_timeline,
glimpse._format_messages, identity_drift._get_recent_messages, and
afterthought's import from relationship_memory."
```

---

### Task 5: 创建 DebouncedPipeline + 迁移 Manager

**Files:**
- Create: `app/services/debounced_pipeline.py`
- Create: `tests/unit/test_debounced_pipeline.py`
- Modify: `app/services/afterthought.py` (AfterthoughtManager 继承 DebouncedPipeline)
- Modify: `app/services/identity_drift.py` (IdentityDriftManager 继承 DebouncedPipeline)

- [ ] **Step 1: Write failing tests for DebouncedPipeline**

```python
# tests/unit/test_debounced_pipeline.py
"""DebouncedPipeline 两阶段防抖管线的单元测试"""
import asyncio

import pytest

from unittest.mock import AsyncMock


class ConcretePipeline:
    """测试用的具体实现"""

    def __init__(self, debounce_seconds: float, max_buffer: int):
        # 延迟 import，因为实现还不存在
        from app.services.debounced_pipeline import DebouncedPipeline

        class _Impl(DebouncedPipeline):
            def __init__(self, ds, mb):
                super().__init__(ds, mb)
                self.processed: list[tuple[str, str, int]] = []

            async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
                self.processed.append((chat_id, persona_id, event_count))

        self._impl = _Impl(debounce_seconds, max_buffer)

    @property
    def impl(self):
        return self._impl


class TestDebouncedPipeline:

    @pytest.mark.asyncio
    async def test_debounce_triggers_after_timeout(self):
        """防抖超时后触发 process"""
        from app.services.debounced_pipeline import DebouncedPipeline

        class Impl(DebouncedPipeline):
            def __init__(self):
                super().__init__(debounce_seconds=0.1, max_buffer=100)
                self.processed = []

            async def process(self, chat_id, persona_id, event_count):
                self.processed.append((chat_id, persona_id, event_count))

        pipeline = Impl()
        await pipeline.on_event("chat1", "akao")
        await pipeline.on_event("chat1", "akao")

        # 等防抖超时
        await asyncio.sleep(0.2)

        assert len(pipeline.processed) == 1
        assert pipeline.processed[0] == ("chat1", "akao", 2)

    @pytest.mark.asyncio
    async def test_max_buffer_forces_flush(self):
        """达到 max_buffer 时立即触发，不等防抖"""
        from app.services.debounced_pipeline import DebouncedPipeline

        class Impl(DebouncedPipeline):
            def __init__(self):
                super().__init__(debounce_seconds=10.0, max_buffer=3)
                self.processed = []

            async def process(self, chat_id, persona_id, event_count):
                self.processed.append(event_count)

        pipeline = Impl()
        for _ in range(3):
            await pipeline.on_event("c", "p")

        # 给 phase2 一点时间执行
        await asyncio.sleep(0.05)

        assert len(pipeline.processed) == 1
        assert pipeline.processed[0] == 3

    @pytest.mark.asyncio
    async def test_events_during_phase2_buffered(self):
        """phase2 运行期间新事件被缓存，phase2 结束后重新触发"""
        from app.services.debounced_pipeline import DebouncedPipeline

        class Impl(DebouncedPipeline):
            def __init__(self):
                super().__init__(debounce_seconds=0.05, max_buffer=100)
                self.processed = []

            async def process(self, chat_id, persona_id, event_count):
                self.processed.append(event_count)
                # 模拟耗时的 phase2
                await asyncio.sleep(0.1)

        pipeline = Impl()
        await pipeline.on_event("c", "p")
        await asyncio.sleep(0.1)  # 等 phase2 开始

        # phase2 运行中发新事件
        await pipeline.on_event("c", "p")
        await pipeline.on_event("c", "p")

        # 等第二轮 phase2 完成
        await asyncio.sleep(0.3)

        assert len(pipeline.processed) == 2
        assert pipeline.processed[0] == 1  # 第一轮
        assert pipeline.processed[1] == 2  # 第二轮（phase2 期间的事件）

    @pytest.mark.asyncio
    async def test_separate_keys_independent(self):
        """不同的 (chat_id, persona_id) 独立防抖"""
        from app.services.debounced_pipeline import DebouncedPipeline

        class Impl(DebouncedPipeline):
            def __init__(self):
                super().__init__(debounce_seconds=0.05, max_buffer=100)
                self.processed = []

            async def process(self, chat_id, persona_id, event_count):
                self.processed.append((chat_id, persona_id))

        pipeline = Impl()
        await pipeline.on_event("c1", "p1")
        await pipeline.on_event("c2", "p2")
        await asyncio.sleep(0.15)

        keys = {(c, p) for c, p, *_ in pipeline.processed}
        assert ("c1", "p1") in keys
        assert ("c2", "p2") in keys
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_debounced_pipeline.py -v`
Expected: FAIL

- [ ] **Step 3: Implement DebouncedPipeline**

```python
# app/services/debounced_pipeline.py
"""两阶段防抖管线基类。

收集事件 → 防抖等待 → 批量处理。
子类只需实现 process() 方法。
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class DebouncedPipeline(ABC):

    def __init__(self, debounce_seconds: float, max_buffer: int):
        self._debounce_seconds = debounce_seconds
        self._max_buffer = max_buffer
        self._buffers: dict[str, int] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._phase2_running: set[str] = set()

    def _key(self, chat_id: str, persona_id: str) -> str:
        return f"{chat_id}:{persona_id}"

    async def on_event(self, chat_id: str, persona_id: str) -> None:
        """收到事件，加入 buffer"""
        key = self._key(chat_id, persona_id)
        self._buffers[key] = self._buffers.get(key, 0) + 1

        if key in self._phase2_running:
            return  # phase2 运行中，只缓存不触发

        if self._buffers[key] >= self._max_buffer:
            # 达到上限，立即 flush
            self._cancel_timer(key)
            asyncio.create_task(self._enter_phase2(chat_id, persona_id))
            return

        # 重置防抖计时器
        self._cancel_timer(key)
        self._timers[key] = asyncio.create_task(
            self._phase1_timer(chat_id, persona_id)
        )

    async def _phase1_timer(self, chat_id: str, persona_id: str) -> None:
        """防抖等待"""
        try:
            await asyncio.sleep(self._debounce_seconds)
            await self._enter_phase2(chat_id, persona_id)
        except asyncio.CancelledError:
            pass

    async def _enter_phase2(self, chat_id: str, persona_id: str) -> None:
        """进入 phase2 处理"""
        key = self._key(chat_id, persona_id)
        event_count = self._buffers.pop(key, 0)
        if event_count == 0:
            return

        self._phase2_running.add(key)
        try:
            await self.process(chat_id, persona_id, event_count)
        except Exception:
            logger.exception("DebouncedPipeline.process failed for %s", key)
        finally:
            self._phase2_running.discard(key)
            # phase2 期间有新事件？重新触发防抖
            if self._buffers.get(key, 0) > 0:
                self._timers[key] = asyncio.create_task(
                    self._phase1_timer(chat_id, persona_id)
                )

    def _cancel_timer(self, key: str) -> None:
        timer = self._timers.pop(key, None)
        if timer and not timer.done():
            timer.cancel()

    @abstractmethod
    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        """子类实现具体的批量处理逻辑"""
```

- [ ] **Step 4: Run tests, migrate AfterthoughtManager + IdentityDriftManager**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_debounced_pipeline.py -v`

迁移 afterthought.py 和 identity_drift.py：将 Manager 类改为继承 DebouncedPipeline，删除重复的 buffer/timer/phase2 逻辑，只保留 `process()` 实现。

Run: `cd apps/agent-service && uv run pytest tests/unit/test_afterthought.py tests/unit/test_identity_drift.py -v`
Run: `cd apps/agent-service && uv run pytest -x -q`

- [ ] **Step 5: Commit**

```bash
git add app/services/debounced_pipeline.py tests/unit/test_debounced_pipeline.py -u
git commit -m "refactor(agent-service): add DebouncedPipeline, deduplicate afterthought + identity_drift managers

Extracted identical two-phase debounce logic into base class.
AfterthoughtManager and IdentityDriftManager now only implement process()."
```

---

### Task 6: 创建 ImageProcessor + 迁移调用方

**Files:**
- Create: `app/agents/tools/image/processor.py`
- Create: `tests/unit/test_image_processor.py`
- Modify: `app/agents/domains/main/context_builder.py` (lines ~76-144)
- Modify: `app/agents/tools/search/image.py` (lines ~81-111)
- Modify: `app/agents/tools/image/generate.py` (lines ~67-89)

- [ ] **Step 1: 读取 3 个图片处理位置的实际代码，提取共同模式**

读取 context_builder.py、search/image.py、image/generate.py 中图片上传注册的精确代码，确认共同步骤：
1. 获取图片 URL
2. 上传到 TOS（调用 image_client）
3. 注册到 ImageRegistry

- [ ] **Step 2: Write failing tests for ImageProcessor**

```python
# tests/unit/test_image_processor.py
"""ImageProcessor 统一图片上传注册的单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestImageProcessor:

    @pytest.mark.asyncio
    async def test_upload_and_register_returns_tos_url(self):
        """上传成功返回 TOS URL"""
        mock_client = MagicMock()
        mock_client.upload_from_url = AsyncMock(return_value="https://tos.example.com/img.jpg")
        mock_registry = MagicMock()

        with patch(
            "app.agents.tools.image.processor.get_image_client",
            return_value=mock_client,
        ):
            from app.agents.tools.image.processor import ImageProcessor

            result = await ImageProcessor.upload_and_register(
                url="https://example.com/img.jpg",
                registry=mock_registry,
            )

        assert result == "https://tos.example.com/img.jpg"
        mock_registry.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_without_registry(self):
        """没有 registry 时只上传不注册"""
        mock_client = MagicMock()
        mock_client.upload_from_url = AsyncMock(return_value="https://tos.example.com/img.jpg")

        with patch(
            "app.agents.tools.image.processor.get_image_client",
            return_value=mock_client,
        ):
            from app.agents.tools.image.processor import ImageProcessor

            result = await ImageProcessor.upload_and_register(
                url="https://example.com/img.jpg",
                registry=None,
            )

        assert result == "https://tos.example.com/img.jpg"

    @pytest.mark.asyncio
    async def test_upload_failure_returns_original_url(self):
        """上传失败时返回原始 URL"""
        mock_client = MagicMock()
        mock_client.upload_from_url = AsyncMock(side_effect=Exception("upload failed"))

        with patch(
            "app.agents.tools.image.processor.get_image_client",
            return_value=mock_client,
        ):
            from app.agents.tools.image.processor import ImageProcessor

            result = await ImageProcessor.upload_and_register(
                url="https://example.com/img.jpg",
                registry=None,
            )

        assert result == "https://example.com/img.jpg"
```

- [ ] **Step 3: Implement ImageProcessor**

```python
# app/agents/tools/image/processor.py
"""统一图片上传注册。

所有需要「获取图片 → TOS 上传 → Registry 注册」的地方统一调用此模块。
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.clients.image_client import get_image_client

if TYPE_CHECKING:
    from app.clients.image_registry import ImageRegistry

logger = logging.getLogger(__name__)


class ImageProcessor:

    @staticmethod
    async def upload_and_register(
        url: str,
        registry: ImageRegistry | None = None,
    ) -> str:
        """下载 → TOS 上传 → 注册，返回 TOS URL。上传失败返回原始 URL。"""
        try:
            client = get_image_client()
            tos_url = await client.upload_from_url(url)
        except Exception:
            logger.warning("Image upload failed for %s, using original URL", url)
            return url

        if registry and tos_url:
            try:
                registry.register(tos_url)
            except Exception:
                logger.warning("Image registry failed for %s", tos_url)

        return tos_url or url
```

- [ ] **Step 4: Run tests, migrate 3 callers, run full suite**

Run: `cd apps/agent-service && uv run pytest tests/unit/test_image_processor.py -v`

迁移 context_builder.py、search/image.py、image/generate.py。

Run: `cd apps/agent-service && uv run pytest -x -q`

- [ ] **Step 5: Commit**

```bash
git add app/agents/tools/image/processor.py tests/unit/test_image_processor.py -u
git commit -m "refactor(agent-service): add ImageProcessor, deduplicate 3 image upload+register patterns

context_builder, search/image, and image/generate now use shared
ImageProcessor.upload_and_register."
```

---

## Phase 3: 数据层

### Task 7: 拆分 CRUD God Object

**Files:**
- Create: `app/orm/crud/` directory with `__init__.py`, `persona.py`, `model_provider.py`, `message.py`, `schedule.py`, `life_engine.py`
- Delete: `app/orm/crud.py` (原文件)
- Create: `tests/unit/test_crud_persona.py`, `tests/unit/test_crud_message.py`, `tests/unit/test_crud_life_engine.py`
- Reference: existing `tests/unit/test_memory_crud.py` (for patterns)

- [ ] **Step 1: Write tests for new CRUD modules**

为新 CRUD 模块写测试，使用现有 `test_memory_crud.py` 的 mock session pattern（`_make_mock_session` + `_make_mock_result`）。

每个新 CRUD 模块的测试：
- `test_crud_persona.py`: test `get_bot_persona`, `resolve_persona_id`, `resolve_bot_name_for_persona`, `get_all_persona_ids`
- `test_crud_message.py`: test `get_message_content`, `get_chat_messages_in_range`, `get_username`, `get_group_name`, `update_agent_response`, `update_safety_status`
- `test_crud_life_engine.py`: test `load_state`, `save_state`

- [ ] **Step 2: 创建 CRUD 子模块**

将 `crud.py` 的函数按实体拆分到对应文件：

| 源函数 | 目标文件 |
|-------|---------|
| `get_bot_persona`, `get_gray_config`, `get_all_persona_ids` | `crud/persona.py` |
| `get_model_and_provider_info`, `parse_model_id` | `crud/model_provider.py` |
| `get_message_content`, `get_chat_messages_in_range`, `get_username` | `crud/message.py` |
| `get_current_schedule`, `get_latest_plan`, `get_plan_for_period`, etc. | `crud/schedule.py` |

另外将散落在 services/workers 中的裸 SQL 提取为 CRUD 函数：

| 源位置 | 新 CRUD 函数 | 目标文件 |
|-------|------------|---------|
| bot_context.py `_resolve_persona_id` | `resolve_persona_id(bot_name)` | `crud/persona.py` |
| bot_context.py `_resolve_bot_name_for_persona` | `resolve_bot_name_for_persona(persona_id, chat_id)` | `crud/persona.py` |
| message_router.py 裸 SQL | `resolve_mentioned_personas(mentions)` | `crud/persona.py` |
| chat_consumer.py UPDATE | `update_agent_response(session_id, bot_name, persona_id)` | `crud/message.py` |
| post_consumer.py UPDATE | `update_safety_status(response_id, status, result)` | `crud/message.py` |
| afterthought.py / glimpse.py | `get_group_name(chat_id)` | `crud/message.py` |
| life_engine.py 直接 session | `load_life_engine_state(persona_id)` / `save_life_engine_state(...)` | `crud/life_engine.py` |
| glimpse_worker.py | `load_life_engine_state` (复用) | `crud/life_engine.py` |
| vectorize_worker.py | `get_message_by_id`, `update_vector_status`, `scan_pending_messages` | `crud/message.py` |
| download_permission.py | `check_download_permission(chat_id)` | `crud/persona.py` |

`crud/__init__.py` re-export 所有公开函数：

```python
# app/orm/crud/__init__.py
"""CRUD re-export layer.

保持 `from app.orm.crud import get_bot_persona` 等现有 import 兼容。
"""
from app.orm.crud.persona import *  # noqa: F401,F403
from app.orm.crud.model_provider import *  # noqa: F401,F403
from app.orm.crud.message import *  # noqa: F401,F403
from app.orm.crud.schedule import *  # noqa: F401,F403
from app.orm.crud.life_engine import *  # noqa: F401,F403
```

- [ ] **Step 3: Run tests**

Run: `cd apps/agent-service && uv run pytest -x -q`
Expected: All pass（re-export 保持兼容）

- [ ] **Step 4: 迁移所有裸 SQL 调用方**

逐个修改 services 和 workers 中直接使用 AsyncSessionLocal 的地方，改为调用新 CRUD 函数。每个文件修改后运行其对应测试。

- [ ] **Step 5: 验证无 services/workers 直接 import AsyncSessionLocal**

Run: `cd apps/agent-service && grep -r "AsyncSessionLocal" app/services/ app/workers/ --include="*.py" -l`
Expected: 空（只有 orm/ 和 clients/ 目录下可以使用）

注意：有些 worker 文件可能有合理的 session 使用（如 RabbitMQ consumer 的 ack 逻辑），这种情况需要逐个判断。

- [ ] **Step 6: 删除旧 crud.py，运行全量测试**

Run: `cd apps/agent-service && uv run pytest -x -q`

- [ ] **Step 7: Commit**

```bash
git add app/orm/crud/ tests/unit/test_crud_*.py -u
git rm app/orm/crud.py
git commit -m "refactor(agent-service): split CRUD god object into per-entity modules

crud.py (316 lines) split into crud/persona.py, crud/model_provider.py,
crud/message.py, crud/schedule.py, crud/life_engine.py.
Consolidated 14+ raw SQL calls from services/workers into CRUD layer.
re-export __init__.py maintains backward compatibility."
```

---

## Phase 4: 编排层

### Task 8: 拆分主 Agent 编排器

**Files:**
- Create: `app/agents/domains/main/safety_race.py`
- Create: `app/agents/domains/main/stream_handler.py`
- Create: `app/agents/domains/main/post_actions.py`
- Modify: `app/agents/domains/main/agent.py` (从 464 行瘦身)
- Create: `tests/unit/test_safety_race.py`
- Create: `tests/unit/test_stream_handler.py`

- [ ] **Step 1: 读取 agent.py 全文，划分拆分边界**

读取 `app/agents/domains/main/agent.py`，确认：
- `_buffer_until_pre` (lines ~119-262) → `safety_race.py`
- stream loop + token counting (lines ~357-406) → `stream_handler.py`
- fire-and-forget 后处理 (lines ~407-431) → `post_actions.py`

- [ ] **Step 2: Write tests for safety_race**

测试竞速逻辑：
- pre 先完成且通过 → stream 正常输出
- pre 先完成且拦截 → 输出 guard message
- stream 先出 token → buffer 住直到 pre 完成

- [ ] **Step 3: Write tests for stream_handler**

测试流式输出：
- 正常 AIMessageChunk 被 yield
- ToolMessage 被跳过
- token 计数正确

- [ ] **Step 4: Implement safety_race.py, stream_handler.py, post_actions.py**

从 agent.py 中提取对应代码段，保持逻辑不变。

- [ ] **Step 5: 瘦身 agent.py**

agent.py 变为：
```python
async def stream_chat(context: AgentContext) -> AsyncGenerator:
    # 1. 启动 pre-safety
    pre_task = asyncio.create_task(run_pre(message))
    # 2. 构建 context
    chat_context = await build_chat_context(...)
    bot_context = await BotContext.from_persona(...)
    # 3. 主 agent stream + safety race
    async for chunk in race_with_pre(pre_task, agent_stream, ...):
        yield chunk
    # 4. 后处理
    fire_post_actions(...)
```

- [ ] **Step 6: Run full test suite**

Run: `cd apps/agent-service && uv run pytest -x -q`

- [ ] **Step 7: Commit**

```bash
git add app/agents/domains/main/ tests/unit/test_safety_race.py tests/unit/test_stream_handler.py
git commit -m "refactor(agent-service): split main agent orchestrator into focused modules

agent.py 464 lines → agent.py (~80) + safety_race.py + stream_handler.py
+ post_actions.py. Each module has single responsibility and is testable."
```

---

### Task 9: Worker 错误处理 + 去重收尾

**Files:**
- Create: `app/workers/error_handling.py`
- Create: `app/agents/graphs/shared/banned_word.py`
- Create: `tests/unit/test_worker_error_handling.py`
- Modify: all workers (套上错误处理装饰器)
- Modify: `app/agents/graphs/pre/nodes/safety.py` (banned_word 改引 shared)
- Modify: `app/agents/graphs/post/safety.py` (banned_word 改引 shared)
- Modify: all tools missing `@tool_error_handler`

- [ ] **Step 1: Write tests for error handling decorators**

```python
# tests/unit/test_worker_error_handling.py
"""Worker 错误处理装饰器的单元测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestCronErrorHandler:

    @pytest.mark.asyncio
    async def test_normal_execution_passes_through(self):
        """正常执行时不干预"""
        from app.workers.error_handling import cron_error_handler

        @cron_error_handler()
        async def my_job(ctx):
            return "done"

        result = await my_job({})
        assert result == "done"

    @pytest.mark.asyncio
    async def test_exception_caught_and_logged(self):
        """异常被捕获并 log，不中断调度"""
        from app.workers.error_handling import cron_error_handler

        @cron_error_handler()
        async def my_job(ctx):
            raise ValueError("boom")

        # 不应抛出异常
        result = await my_job({})
        assert result is None


class TestMqErrorHandler:

    @pytest.mark.asyncio
    async def test_normal_execution_acks(self):
        """正常执行时 ack 消息"""
        from app.workers.error_handling import mq_error_handler

        @mq_error_handler()
        async def handle(message):
            return "ok"

        mock_msg = MagicMock()
        mock_msg.ack = AsyncMock()
        result = await handle(mock_msg)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_exception_nacks_message(self):
        """异常时 nack 消息"""
        from app.workers.error_handling import mq_error_handler

        @mq_error_handler()
        async def handle(message):
            raise RuntimeError("fail")

        mock_msg = MagicMock()
        mock_msg.nack = AsyncMock()
        await handle(mock_msg)
```

- [ ] **Step 2: Implement error_handling.py**

```python
# app/workers/error_handling.py
"""Worker 统一错误处理装饰器。"""
from __future__ import annotations

import functools
import logging

logger = logging.getLogger(__name__)


def cron_error_handler():
    """arq cron job 错误处理：log + 不中断调度"""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception:
                logger.exception("Cron job %s failed", func.__name__)
                return None

        return wrapper

    return decorator


def mq_error_handler():
    """MQ consumer 错误处理：log + nack"""

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(message, *args, **kwargs):
            try:
                return await func(message, *args, **kwargs)
            except Exception:
                logger.exception("MQ handler %s failed", func.__name__)
                if hasattr(message, "nack"):
                    await message.nack(requeue=False)
                return None

        return wrapper

    return decorator
```

- [ ] **Step 3: Extract shared banned_word check**

```python
# app/agents/graphs/shared/banned_word.py
"""共享的 banned word 检查逻辑。pre 和 post safety 共用。"""
from app.services.banned_word import check_banned_word  # 复用现有实现

__all__ = ["check_banned_word"]
```

修改 pre/nodes/safety.py 和 post/safety.py，改为从 `app.agents.graphs.shared.banned_word` import。

- [ ] **Step 4: 补齐 tool_error_handler 到所有工具**

找到所有缺少 `@tool_error_handler` 的工具函数，补上装饰器。

- [ ] **Step 5: 套 worker 错误处理装饰器**

给 dream_worker、schedule_worker、voice_worker、glimpse_worker、life_engine_worker 的 cron handler 套上 `@cron_error_handler()`。
给 chat_consumer、post_consumer 的 message handler 套上 `@mq_error_handler()`。

- [ ] **Step 6: 移动 content_parser.py**

将 `app/utils/content_parser.py` 移到 `app/services/content_parser.py`（业务逻辑不属于 utils）。更新所有 import。

- [ ] **Step 7: Run full test suite**

Run: `cd apps/agent-service && uv run pytest -x -q`

- [ ] **Step 8: Commit**

```bash
git add app/workers/error_handling.py app/agents/graphs/shared/ tests/unit/test_worker_error_handling.py -u
git commit -m "refactor(agent-service): add worker error handling, deduplicate banned_word, fix tool_error_handler coverage

- cron_error_handler and mq_error_handler decorators for all workers
- Shared banned_word check for pre/post safety
- tool_error_handler applied to all tools
- content_parser moved from utils to services"
```

---

## Final Verification

### Task 10: 全量验证 + 清理

- [ ] **Step 1: Run full test suite**

Run: `cd apps/agent-service && uv run pytest -v`
Expected: All pass

- [ ] **Step 2: Run linter**

Run: `cd apps/agent-service && uv run ruff check app/ tests/`
Expected: No errors

- [ ] **Step 3: 验证 import 约束**

```bash
# services/ 和 workers/ 不应直接 import AsyncSessionLocal
grep -r "AsyncSessionLocal" app/services/ app/workers/ --include="*.py" -l
# 期望：空

# services/ 和 workers/ 不应直接 import ModelBuilder（除了 llm_service.py）
grep -r "ModelBuilder" app/services/ app/workers/ --include="*.py" -l
# 期望：空

# 不应有直接 import ChatAgent（除了 llm_service.py）
grep -r "from app.agents.core.agent import ChatAgent" app/services/ app/workers/ --include="*.py" -l
# 期望：空
```

- [ ] **Step 4: 确认无残留旧代码**

检查以下函数/方法已被删除：
- `relationship_memory.format_timeline` (迁移到 timeline_formatter)
- `glimpse._format_messages` (迁移到 timeline_formatter)
- `identity_drift._get_recent_messages` (迁移到 timeline_formatter)
- `identity_drift._get_persona_context` (迁移到 persona_loader)
- `glimpse._get_persona_info` (迁移到 persona_loader)
- `bot_context._resolve_persona_id` (迁移到 crud/persona)
- `bot_context._resolve_bot_name_for_persona` (迁移到 crud/persona)
- 旧 `app/orm/crud.py` (已拆分为 crud/)

- [ ] **Step 5: Commit 任何清理**

```bash
git add -u
git commit -m "chore(agent-service): final cleanup after architecture refactor"
```

- [ ] **Step 6: Push**

```bash
git push origin refactor/agent-service-architecture
```
