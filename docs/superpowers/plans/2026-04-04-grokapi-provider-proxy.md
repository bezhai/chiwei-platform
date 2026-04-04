# GrokAPI Provider + Provider-Level Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接入 GrokAPI (xAI) 作为新 provider（走 `openai-responses` client_type），并为 `model_provider` 表增加 `use_proxy` 字段，让每个 provider 自行决定是否使用正向代理。

**Architecture:** 在 `model_provider` 表加 `use_proxy BOOLEAN DEFAULT FALSE` 列。CRUD 层把该字段传递到 `model_info` dict。ModelBuilder 和低层 Client 在构建 HTTP 客户端时，根据 `use_proxy + settings.forward_proxy_url` 决定是否注入代理。Google 分支从硬编码全局判断改为按 provider 判断。

**Tech Stack:** Python, SQLAlchemy ORM, LangChain (ChatOpenAI), OpenAI SDK (AsyncOpenAI), httpx, google-genai SDK, PostgreSQL

---

### Task 1: ORM — `ModelProvider` 加 `use_proxy` 字段

**Files:**
- Modify: `apps/agent-service/app/orm/models.py:33-44`

- [ ] **Step 1: 给 ModelProvider 加 use_proxy 列**

在 `ModelProvider` 类的 `is_active` 字段后面加一行：

```python
use_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
```

完整的 `ModelProvider` 类变为：

```python
class ModelProvider(Base):
    __tablename__ = "model_provider"

    provider_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    api_key: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    client_type: Mapped[str] = mapped_column(String(50), default="openai")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    use_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
```

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/orm/models.py
git commit -m "feat(orm): add use_proxy field to ModelProvider"
```

---

### Task 2: CRUD — 返回 `use_proxy` 字段

**Files:**
- Modify: `apps/agent-service/app/orm/crud.py:82-88`

- [ ] **Step 1: 在 get_model_and_provider_info 返回值中加 use_proxy**

将 `crud.py` 第 82-88 行的返回 dict 改为：

```python
        return {
            "model_name": actual_model_name,
            "api_key": provider.api_key,
            "base_url": provider.base_url,
            "is_active": provider.is_active,
            "client_type": provider.client_type or "openai",
            "use_proxy": provider.use_proxy,
        }
```

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/orm/crud.py
git commit -m "feat(crud): propagate use_proxy in model_info dict"
```

---

### Task 3: ModelBuilder — proxy 注入 + `get_basic_model_params` 传递

**Files:**
- Modify: `apps/agent-service/app/agents/infra/model_builder.py:151-306`

- [ ] **Step 1: `get_basic_model_params` 传递 `use_proxy`**

修改 `get_basic_model_params` 返回值（第 173-178 行），在 `client_type` 后加 `use_proxy`：

```python
        return {
            "api_key": model_info["api_key"],
            "base_url": model_info["base_url"],
            "model": model_info["model_name"],
            "client_type": model_info["client_type"],
            "use_proxy": model_info.get("use_proxy", False),
        }
```

- [ ] **Step 2: 抽取 proxy httpx client 辅助函数**

在 `ModelBuilder` 类之前（`clear_model_info_cache` 函数后），添加一个模块级辅助函数：

```python
def _make_proxy_async_client() -> "httpx.AsyncClient | None":
    """如果全局正向代理已配置，返回带 proxy 的 httpx.AsyncClient，否则返回 None。"""
    from app.config.config import settings

    if not settings.forward_proxy_url:
        return None
    import httpx
    return httpx.AsyncClient(proxy=settings.forward_proxy_url)
```

- [ ] **Step 3: `openai` 默认分支注入 proxy**

修改 `build_chat_model` 中 `else` 分支（第 293-306 行）：

```python
            else:
                # openai 及其他: 标准 Chat Completions API
                chat_params = {
                    "api_key": model_info["api_key"],
                    "base_url": model_info["base_url"],
                    "model": model_info["model_name"],
                    "max_retries": max_retries,
                    "use_responses_api": False,
                    **kwargs,
                }
                if model_info.get("use_proxy"):
                    http_async_client = _make_proxy_async_client()
                    if http_async_client:
                        chat_params["http_async_client"] = http_async_client
                logger.info(
                    f"为模型 {model_id} 构建ChatOpenAI（Completions API）"
                )
                return ChatOpenAI(**chat_params)
```

- [ ] **Step 4: `openai-responses` 分支注入 proxy**

修改 `build_chat_model` 中 `openai-responses` 分支（第 267-279 行）：

```python
            elif client_type == "openai-responses":
                chat_params = {
                    "api_key": model_info["api_key"],
                    "base_url": model_info["base_url"],
                    "model": model_info["model_name"],
                    "max_retries": max_retries,
                    "use_responses_api": True,
                    **kwargs,
                }
                if model_info.get("use_proxy"):
                    http_async_client = _make_proxy_async_client()
                    if http_async_client:
                        chat_params["http_async_client"] = http_async_client
                logger.info(
                    f"为模型 {model_id} 构建ChatOpenAI（Responses API）"
                )
                return ChatOpenAI(**chat_params)
```

- [ ] **Step 5: `deepseek` 分支注入 proxy**

修改 `build_chat_model` 中 `deepseek` 分支（第 280-292 行）：

```python
            elif client_type == "deepseek":
                chat_params = {
                    "api_key": model_info["api_key"],
                    "base_url": model_info["base_url"],
                    "model": model_info["model_name"],
                    "max_retries": max_retries,
                    "use_responses_api": False,
                    **kwargs,
                }
                if model_info.get("use_proxy"):
                    http_async_client = _make_proxy_async_client()
                    if http_async_client:
                        chat_params["http_async_client"] = http_async_client
                logger.info(
                    f"为模型 {model_id} 构建DeepSeek ChatOpenAI（Completions API）"
                )
                return _ReasoningChatOpenAI(**chat_params)
```

- [ ] **Step 6: `google` 分支改用 `use_proxy` 判断**

修改 `build_chat_model` 中 `google` 分支（第 244-266 行），将 `if settings.forward_proxy_url:` 改为 `if model_info.get("use_proxy") and settings.forward_proxy_url:`：

```python
            elif client_type == "google":
                from langchain_google_genai import ChatGoogleGenerativeAI

                from app.config.config import settings

                chat_params = {
                    "api_key": model_info["api_key"],
                    "base_url": model_info["base_url"],
                    "model": model_info["model_name"],
                    "max_retries": max_retries,
                    **kwargs,
                }
                if model_info.get("use_proxy") and settings.forward_proxy_url:
                    chat_params["client_args"] = {
                        "proxy": settings.forward_proxy_url
                    }

                logger.info(
                    f"为模型 {model_id} 构建ChatGoogleGenerativeAI实例，"
                    f"参数: {list(chat_params.keys())}"
                )

                return ChatGoogleGenerativeAI(**chat_params)
```

- [ ] **Step 7: Commit**

```bash
git add apps/agent-service/app/agents/infra/model_builder.py
git commit -m "feat(model-builder): inject proxy for openai/google based on use_proxy"
```

---

### Task 4: 低层 Client — `OpenAIClient` 补 proxy 支持

**Files:**
- Modify: `apps/agent-service/app/agents/clients/openai_client.py:13-19`

- [ ] **Step 1: 在 _create_client 中注入 proxy**

修改 `OpenAIClient._create_client` 方法：

```python
    async def _create_client(self, model_info: dict) -> AsyncOpenAI:
        """创建 AsyncOpenAI 客户端实例。"""
        kwargs: dict = {
            "api_key": model_info["api_key"],
            "base_url": model_info["base_url"],
            "timeout": 60.0,
            "max_retries": 3,
        }
        if model_info.get("use_proxy"):
            from app.agents.infra.model_builder import _make_proxy_async_client

            http_client = _make_proxy_async_client()
            if http_client:
                kwargs["http_client"] = http_client
        return AsyncOpenAI(**kwargs)
```

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/agents/clients/openai_client.py
git commit -m "feat(openai-client): support proxy via use_proxy flag"
```

---

### Task 5: 低层 Client — `GeminiClient` 改用 `use_proxy`

**Files:**
- Modify: `apps/agent-service/app/agents/clients/gemini_client.py:23-35`

- [ ] **Step 1: 改 _create_client 判断逻辑**

修改 `GeminiClient._create_client` 方法，将 `if settings.forward_proxy_url:` 改为 `if model_info.get("use_proxy") and settings.forward_proxy_url:`：

```python
    async def _create_client(self, model_info: dict) -> genai.Client:
        from app.config.config import settings

        http_opts: dict = {}
        if model_info.get("base_url"):
            http_opts["base_url"] = model_info["base_url"]
        if model_info.get("use_proxy") and settings.forward_proxy_url:
            http_opts["client_args"] = {"proxy": settings.forward_proxy_url}

        return genai.Client(
            api_key=model_info["api_key"],
            http_options=types.HttpOptions(**http_opts) if http_opts else None,
        )
```

- [ ] **Step 2: Commit**

```bash
git add apps/agent-service/app/agents/clients/gemini_client.py
git commit -m "feat(gemini-client): use use_proxy flag instead of global check"
```

---

### Task 6: 测试 — proxy 注入逻辑

**Files:**
- Modify: `apps/agent-service/tests/conftest.py:56-81`
- Create: `apps/agent-service/tests/unit/test_proxy_injection.py`

- [ ] **Step 1: 更新 model_info_factory 加 use_proxy 默认值**

修改 `tests/conftest.py` 中的 `_factory` 函数签名，加入 `use_proxy: bool = False`：

```python
    def _factory(
        *,
        model_id: str = "test-model",
        model_name: str = "gpt-4o-mini",
        api_key: str = "sk-test-key",
        base_url: str = "https://api.test.com/v1",
        client_type: str = "openai-http",
        is_active: bool = True,
        use_proxy: bool = False,
        **overrides: Any,
    ) -> dict[str, Any]:
        info = {
            "model_id": model_id,
            "model_name": model_name,
            "api_key": api_key,
            "base_url": base_url,
            "client_type": client_type,
            "is_active": is_active,
            "use_proxy": use_proxy,
        }
        info.update(overrides)
        return info
```

- [ ] **Step 2: 编写 proxy 注入测试**

创建 `apps/agent-service/tests/unit/test_proxy_injection.py`：

```python
"""test_proxy_injection.py — use_proxy 标志位的 proxy 注入测试

覆盖场景：
- use_proxy=True + forward_proxy_url 存在 → 注入 proxy
- use_proxy=False → 不注入 proxy
- use_proxy=True + forward_proxy_url 为 None → 不注入 proxy
"""

from unittest.mock import AsyncMock, patch

import pytest

from app.agents.infra.model_builder import ModelBuilder

pytestmark = pytest.mark.unit


def _make_model_info(*, client_type: str = "openai", use_proxy: bool = False):
    return {
        "model_name": "test-model",
        "api_key": "sk-test",
        "base_url": "https://api.test.com/v1",
        "is_active": True,
        "client_type": client_type,
        "use_proxy": use_proxy,
    }


class TestOpenAIProxyInjection:
    """openai / openai-responses / deepseek 分支的 proxy 注入"""

    @pytest.mark.parametrize("client_type", ["openai", "openai-responses", "deepseek"])
    async def test_proxy_injected_when_use_proxy_true(self, client_type):
        info = _make_model_info(client_type=client_type, use_proxy=True)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
        ):
            mock_settings.forward_proxy_url = "http://proxy:7890"
            model = await ModelBuilder.build_chat_model("test")

        # ChatOpenAI 实例应该有 http_async_client
        assert model.http_async_client is not None
        # 清理 httpx client
        await model.http_async_client.aclose()

    @pytest.mark.parametrize("client_type", ["openai", "openai-responses", "deepseek"])
    async def test_no_proxy_when_use_proxy_false(self, client_type):
        info = _make_model_info(client_type=client_type, use_proxy=False)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
        ):
            mock_settings.forward_proxy_url = "http://proxy:7890"
            model = await ModelBuilder.build_chat_model("test")

        assert model.http_async_client is None

    @pytest.mark.parametrize("client_type", ["openai", "openai-responses", "deepseek"])
    async def test_no_proxy_when_proxy_url_none(self, client_type):
        info = _make_model_info(client_type=client_type, use_proxy=True)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
        ):
            mock_settings.forward_proxy_url = None
            model = await ModelBuilder.build_chat_model("test")

        assert model.http_async_client is None


class TestGoogleProxyInjection:
    """google 分支的 proxy 注入"""

    async def test_proxy_injected_when_use_proxy_true(self):
        info = _make_model_info(client_type="google", use_proxy=True)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
            patch(
                "app.agents.infra.model_builder.ChatGoogleGenerativeAI"
            ) as MockGoogle,
        ):
            mock_settings.forward_proxy_url = "http://proxy:7890"
            await ModelBuilder.build_chat_model("test")

        call_kwargs = MockGoogle.call_args.kwargs
        assert call_kwargs.get("client_args") == {"proxy": "http://proxy:7890"}

    async def test_no_proxy_when_use_proxy_false(self):
        info = _make_model_info(client_type="google", use_proxy=False)

        with (
            patch.object(
                ModelBuilder, "_get_model_and_provider_info",
                new=AsyncMock(return_value=info),
            ),
            patch("app.config.config.settings") as mock_settings,
            patch(
                "app.agents.infra.model_builder.ChatGoogleGenerativeAI"
            ) as MockGoogle,
        ):
            mock_settings.forward_proxy_url = "http://proxy:7890"
            await ModelBuilder.build_chat_model("test")

        call_kwargs = MockGoogle.call_args.kwargs
        assert "client_args" not in call_kwargs
```

- [ ] **Step 3: 运行测试**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_proxy_injection.py -v
```

Expected: 全部通过（9 个测试）

- [ ] **Step 4: 运行已有测试确保无回归**

```bash
cd apps/agent-service && uv run pytest tests/unit/test_model_builder.py -v
```

Expected: 全部通过（8 个测试）

- [ ] **Step 5: Commit**

```bash
git add apps/agent-service/tests/conftest.py apps/agent-service/tests/unit/test_proxy_injection.py
git commit -m "test: add proxy injection tests for use_proxy flag"
```

---

### Task 7: DB DDL + DML — 加列 + 插入 Grok provider

**Files:** 无代码文件（通过 ops-db skill 执行）

- [ ] **Step 1: DDL — model_provider 表加 use_proxy 列**

通过 `/ops-db` 执行：

```sql
ALTER TABLE model_provider ADD COLUMN use_proxy BOOLEAN NOT NULL DEFAULT FALSE;
```

- [ ] **Step 2: DML — 更新 Google provider 的 use_proxy**

```sql
UPDATE model_provider SET use_proxy = TRUE WHERE name = 'google';
```

- [ ] **Step 3: DML — 插入 Grok provider**

```sql
INSERT INTO model_provider (provider_id, name, api_key, base_url, client_type, is_active, use_proxy, created_at, updated_at)
VALUES (gen_random_uuid(), 'grok', '<GROK_API_KEY>', 'https://api.x.ai/v1', 'openai-responses', TRUE, TRUE, NOW(), NOW());
```

> **注意：** `<GROK_API_KEY>` 需替换为实际的 xAI API Key，由用户提供。

---
