# agent-service 架构重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按目标架构重写 agent-service，统一 agent 抽象，代码量 -30%（15,414 → ≤10,800 行）

**Architecture:** 9 个顶层模块（agent/chat/memory/life/data/infra/workers/skills/api），以 agent 为核心。所有"思考"走 agent，禁止裸调 LLM（embedding 和画图除外）。详见 `docs/2026-04-11-arch-redesign.md`。

**Tech Stack:** Python 3.11, FastAPI, LangChain/LangGraph, SQLAlchemy (async), ARQ, RabbitMQ (aio-pika), Qdrant, Redis, Langfuse, Pydantic v2

**Ruff 配置：** `ruff.toml` — select E/W/F/I/B/C4/UP, line-length 88, target py311

**测试框架：** pytest, asyncio_mode=auto, markers: unit/integration/slow/api

---

## 总体执行顺序

按依赖关系从底向上构建，每完成一个 Task 立即 commit + ruff check + pytest。

```
Phase A: 基础层
  Task 1: infra/     ← 无新模块依赖
  Task 2: data/      ← 依赖 infra/config

Phase B: 核心抽象
  Task 3: agent/core + models + prompts + tracing  ← 依赖 data/
  Task 4: agent/embedding                          ← 依赖 agent/models
  Task 5: agent/tools/                             ← 依赖 agent/core

Phase C: 领域模块
  Task 6: memory/    ← 依赖 agent/, data/
  Task 7: life/      ← 依赖 agent/, data/
  Task 8: chat/      ← 依赖 agent/, memory/, life/, data/

Phase D: 编排层
  Task 9:  workers/  ← 依赖所有领域模块
  Task 10: api/      ← 依赖 chat/, life/

Phase E: 清扫
  Task 11: 删旧代码 + grep 验证 + 最终 ruff + 行数验收
```

---

## Phase A: 基础层

### Task 1: `infra/` — 外部系统客户端 + 配置

将 `app/config/`, `app/clients/`, `app/services/qdrant.py` 合并为 `app/infra/`。全部改为模块级实例，消灭 `get_instance()` 模式。

**Files:**
- Create: `app/infra/__init__.py`
- Create: `app/infra/config.py` (from `app/config/config.py`)
- Create: `app/infra/redis.py` (from `app/clients/redis.py`)
- Create: `app/infra/rabbitmq.py` (from `app/clients/rabbitmq.py`)
- Create: `app/infra/qdrant.py` (from `app/services/qdrant.py`)
- Create: `app/infra/image.py` (from `app/clients/image_client.py` + `app/clients/image_registry.py`)
- Create: `app/infra/lane.py` (from `app/clients/lane_router_instance.py`)
- Test: `tests/unit/infra/test_config.py`
- Test: `tests/unit/infra/test_rabbitmq.py`

**设计要点：**
- `config.py`: 直接搬 settings 实例，确保 `from app.infra.config import settings` 可用
- `redis.py`: 模块级 `redis_client` 实例，提供 `async def get_redis() -> Redis`（lazy init with asyncio.Lock）
- `rabbitmq.py`: 模块级 `mq_client` 实例，保留泳道隔离和 DLX 设计，去掉 `get_instance()` classmethod
- `qdrant.py`: 模块级 `qdrant` 实例，保留 `init_collections()`、`search_vectors()`、`hybrid_search()` 等接口
- `image.py`: 合并 `image_client.py`（处理/上传/下载）和 `image_registry.py`（Redis Hash 编号），模块级 `image_client` 实例
- `lane.py`: 保持原样，模块级 `lane_router` 实例

- [ ] **Step 1: 创建 `app/infra/config.py`**

从 `app/config/config.py` 重写。保留 Settings dataclass 和 settings 实例，去掉不必要的间接层。

```python
# app/infra/config.py
"""应用配置 — 从环境变量加载"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    """不可变配置，从环境变量读取"""

    # Database
    database_url: str = field(default_factory=lambda: os.environ.get("DATABASE_URL", ""))

    # Redis
    redis_host: str = field(default_factory=lambda: os.environ.get("REDIS_HOST", "localhost"))
    redis_password: str = field(default_factory=lambda: os.environ.get("REDIS_PASSWORD", ""))

    # RabbitMQ
    rabbitmq_url: str = field(default_factory=lambda: os.environ.get("RABBITMQ_URL", ""))

    # Lane
    lane: str = field(default_factory=lambda: os.environ.get("LANE", ""))

    # Qdrant
    qdrant_host: str = field(default_factory=lambda: os.environ.get("QDRANT_HOST", "localhost"))
    qdrant_port: int = field(default_factory=lambda: int(os.environ.get("QDRANT_PORT", "6333")))

    # Models (默认模型 ID，对应 DB 中的 model_mapping)
    diary_model: str = field(default_factory=lambda: os.environ.get("DIARY_MODEL", "diary-model"))
    offline_model: str = field(default_factory=lambda: os.environ.get("OFFLINE_MODEL", "offline-model"))
    relationship_model: str = field(default_factory=lambda: os.environ.get("RELATIONSHIP_MODEL", "relationship-model"))

    # Identity drift
    identity_drift_debounce_seconds: int = field(
        default_factory=lambda: int(os.environ.get("IDENTITY_DRIFT_DEBOUNCE_SECONDS", "600"))
    )
    identity_drift_max_buffer: int = field(
        default_factory=lambda: int(os.environ.get("IDENTITY_DRIFT_MAX_BUFFER", "20"))
    )

    # Proxy
    forward_proxy_url: str = field(default_factory=lambda: os.environ.get("FORWARD_PROXY_URL", ""))


settings = Settings()
```

> **行为参考：** `app/config/config.py` — 逐字段对照，确保不遗漏任何环境变量。完整字段列表以旧文件为准，上面只列出了关键字段作为结构示例。

- [ ] **Step 2: 创建 `app/infra/redis.py`**

```python
# app/infra/redis.py
"""Redis 客户端 — 模块级 lazy 初始化"""

from __future__ import annotations

import asyncio

from redis.asyncio import ConnectionPool, Redis

from app.infra.config import settings

_lock = asyncio.Lock()
_client: Redis | None = None


async def get_redis() -> Redis:
    """获取 Redis 客户端（首次调用时初始化连接池）"""
    global _client
    if _client is not None:
        return _client
    async with _lock:
        if _client is not None:
            return _client
        pool = ConnectionPool(
            host=settings.redis_host,
            port=6379,
            password=settings.redis_password or None,
            decode_responses=True,
        )
        _client = Redis(connection_pool=pool)
        return _client
```

- [ ] **Step 3: 创建 `app/infra/rabbitmq.py`**

从 `app/clients/rabbitmq.py` 重写。保留泳道隔离、DLX 设计。去掉 classmethod singleton，改为模块级实例。

```python
# app/infra/rabbitmq.py (关键接口)
"""RabbitMQ 客户端 — 泳道隔离 + DLX"""

from __future__ import annotations
# ... 省略 import

class RabbitMQ:
    """RabbitMQ 客户端，支持泳道隔离"""

    def __init__(self, url: str, lane: str = ""):
        self._url = url
        self._lane = lane
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None

    def queue_name(self, base: str, *, lane: str | None = None) -> str:
        """生成泳道隔离的队列名"""
        effective_lane = lane if lane is not None else self._lane
        return f"{base}_{effective_lane}" if effective_lane else base

    async def connect(self) -> None: ...
    async def declare_topology(self) -> None: ...
    async def publish(self, queue_base: str, payload: dict, *, lane: str | None = None) -> None: ...
    async def consume(self, queue_base: str, callback, *, prefetch: int = 1) -> None: ...
    async def close(self) -> None: ...

# 模块级实例
mq = RabbitMQ(url=settings.rabbitmq_url, lane=settings.lane)
```

> **行为参考：** `app/clients/rabbitmq.py` 完整实现 — 逐方法对照，确保 DLX/DLQ、lazy declaration、lane 隔离行为不变。

- [ ] **Step 4: 创建 `app/infra/qdrant.py`**

从 `app/services/qdrant.py` 重写。模块级实例。

```python
# app/infra/qdrant.py (关键接口)
"""Qdrant 向量数据库客户端"""

from qdrant_client import AsyncQdrantClient
from app.infra.config import settings

_client = AsyncQdrantClient(host=settings.qdrant_host, port=settings.qdrant_port)

async def init_collections() -> None:
    """启动时确保必要的 collection 存在"""
    ...

async def upsert_vectors(collection: str, points: list) -> None: ...
async def search_vectors(collection: str, vector: list[float], *, limit: int = 5, filters: dict | None = None) -> list: ...
async def hybrid_search(collection: str, dense: list[float], sparse: dict, *, limit: int = 5) -> list: ...
```

> **行为参考：** `app/services/qdrant.py` 完整实现

- [ ] **Step 5: 创建 `app/infra/image.py`**

合并 `image_client.py` + `image_registry.py`。

```python
# app/infra/image.py (关键接口)
"""图片服务客户端 — 飞书下载/TOS 上传/编号注册"""

class ImageClient:
    """图片处理、上传、下载"""
    async def process_image(self, image_key: str) -> str | None: ...
    async def upload_to_tos(self, image_key: str, data: bytes) -> str | None: ...
    async def download_as_base64(self, image_key: str) -> str | None: ...

class ImageRegistry:
    """per-message 图片编号注册表（Redis Hash）"""
    def __init__(self, message_id: str): ...
    async def register(self, image_key: str) -> str: ...  # 返回 "@1.png" 格式
    async def get_all(self) -> dict[str, str]: ...

image_client = ImageClient()
```

> **行为参考：** `app/clients/image_client.py` + `app/clients/image_registry.py`

- [ ] **Step 6: 创建 `app/infra/lane.py`**

```python
# app/infra/lane.py
"""LaneRouter 实例"""
from inner_shared.lane_router import LaneRouter
from app.infra.config import settings

def _get_lane() -> str:
    return settings.lane

lane_router = LaneRouter(lane_provider=_get_lane)
```

- [ ] **Step 7: 创建 `app/infra/__init__.py`**

```python
# app/infra/__init__.py
"""基础设施层 — 外部系统客户端"""
```

- [ ] **Step 8: 写测试验证 config 加载**

```python
# tests/unit/infra/test_config.py
from app.infra.config import Settings

def test_settings_frozen():
    s = Settings()
    try:
        s.lane = "test"  # type: ignore
        assert False, "Should be frozen"
    except AttributeError:
        pass

def test_settings_defaults():
    s = Settings()
    assert s.redis_host == "localhost"
    assert s.lane == ""
```

- [ ] **Step 9: 运行测试 + ruff**

```bash
cd apps/agent-service
uv run ruff check app/infra/ --fix
uv run ruff format app/infra/
uv run pytest tests/unit/infra/ -v
```

- [ ] **Step 10: Commit**

```bash
git add app/infra/ tests/unit/infra/
git commit -m "refactor(agent-service): 新建 infra/ 模块 — 统一外部系统客户端"
```

---

### Task 2: `data/` — 数据模型 + 查询

合并 `app/orm/` 为 `app/data/`。模型合并为一个文件，查询按领域分组，业务逻辑上移。

**Files:**
- Create: `app/data/__init__.py`
- Create: `app/data/models.py` (合并 `orm/models.py` + `orm/memory_models.py`)
- Create: `app/data/session.py` (from `orm/base.py`，增强事务支持)
- Create: `app/data/queries.py` (合并 `orm/crud/*.py` + `orm/memory_crud.py`，去掉业务逻辑)
- Test: `tests/unit/data/test_session.py`

- [ ] **Step 1: 创建 `app/data/session.py`**

```python
# app/data/session.py
"""数据库会话管理"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infra.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话，自动提交/回滚"""
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

- [ ] **Step 2: 创建 `app/data/models.py`**

合并 `orm/models.py` + `orm/memory_models.py` 为一个文件。保留所有表定义，不改表结构。

> **行为参考：** `app/orm/models.py`（14 个模型）+ `app/orm/memory_models.py`（5 个模型）— 逐模型对照，所有字段、索引、约束必须一致。

- [ ] **Step 3: 创建 `app/data/queries.py`**

合并所有 CRUD 文件。关键改动：
- 所有函数接受 `session: AsyncSession` 参数（调用方通过 `get_session()` 管理），不再每个函数自建 session
- 纯查询/写入，不含业务逻辑（如 model_provider fallback、schedule 优先级匹配上移到调用方）
- 按用途分区注释（# --- Chat queries ---、# --- Memory queries ---、# --- Life queries ---）

```python
# app/data/queries.py (结构示例)
"""数据查询函数 — 纯数据访问，不含业务逻辑"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.data.models import (
    BotPersona,
    ConversationMessage,
    ExperienceFragment,
    # ...
)

# --- Persona ---

async def get_persona(session: AsyncSession, persona_id: str) -> BotPersona | None:
    result = await session.execute(select(BotPersona).where(BotPersona.persona_id == persona_id))
    return result.scalar_one_or_none()

async def get_all_persona_ids(session: AsyncSession) -> list[str]:
    result = await session.execute(select(BotPersona.persona_id))
    return list(result.scalars().all())

# --- Chat ---

async def get_message(session: AsyncSession, message_id: str) -> ConversationMessage | None: ...
async def get_message_content(session: AsyncSession, message_id: str) -> str | None: ...
async def get_chat_messages_in_range(session: AsyncSession, chat_id: str, start_ts: int, end_ts: int) -> list[ConversationMessage]: ...
async def get_gray_config(session: AsyncSession, message_id: str) -> dict | None: ...
async def get_group_name(session: AsyncSession, chat_id: str) -> str | None: ...
async def get_username(session: AsyncSession, user_id: str) -> str | None: ...

# --- Memory ---

async def create_fragment(session: AsyncSession, fragment: ExperienceFragment) -> None: ...
async def get_recent_fragments(session: AsyncSession, persona_id: str, grain: str, limit: int = 10) -> list[ExperienceFragment]: ...
async def save_relationship_memory(session: AsyncSession, ...) -> None: ...
async def get_relationship_memories(session: AsyncSession, persona_id: str, user_ids: list[str]) -> list: ...

# --- Life ---

async def get_life_state(session: AsyncSession, persona_id: str) -> ...: ...
async def save_life_state(session: AsyncSession, ...) -> None: ...
async def get_current_schedule(session: AsyncSession, persona_id: str) -> ...: ...
async def upsert_schedule(session: AsyncSession, ...) -> None: ...

# --- Model Provider ---

async def get_model_info(session: AsyncSession, model_id: str) -> dict | None:
    """纯查询：alias → provider + model_name + api_key + base_url"""
    ...
```

> **行为参考：** `app/orm/crud/` 目录所有文件 + `app/orm/memory_crud.py` — 逐函数对照，确保查询行为一致。唯一改动是：函数签名增加 session 参数，去掉业务逻辑。

- [ ] **Step 4: 写测试**

```python
# tests/unit/data/test_session.py
import pytest
from unittest.mock import AsyncMock, patch

@pytest.mark.unit
async def test_get_session_commits_on_success():
    """session 正常结束自动 commit"""
    from app.data.session import get_session
    # 测试 context manager 行为
    ...

@pytest.mark.unit
async def test_get_session_rollbacks_on_error():
    """session 异常时自动 rollback"""
    ...
```

- [ ] **Step 5: ruff + commit**

```bash
uv run ruff check app/data/ --fix && uv run ruff format app/data/
uv run pytest tests/unit/data/ -v
git add app/data/ tests/unit/data/
git commit -m "refactor(agent-service): 新建 data/ 模块 — 模型合并 + 查询按领域分组"
```

---

## Phase B: 核心抽象

### Task 3: `agent/` 核心 — 统一思考入口

**这是整个重构最关键的一步。** 将 ChatAgent 和 LLMService 统一为一个 Agent 抽象。

**Files:**
- Create: `app/agent/__init__.py`
- Create: `app/agent/core.py`
- Create: `app/agent/models.py`
- Create: `app/agent/prompts.py`
- Create: `app/agent/tracing.py`
- Test: `tests/unit/agent/test_core.py`
- Test: `tests/unit/agent/test_models.py`

**设计决策：**
- `Agent` 类取代 `ChatAgent` + `LLMService`
- 有 tools → 走 LangGraph agent loop（多步推理）
- 没 tools → 走单次 `model.ainvoke()`（等价于旧 LLMService.run）
- `response_model` 参数 → 走 `model.with_structured_output()`（等价于旧 LLMService.extract）
- 重试逻辑只写一次
- AgentRegistry 保留，简化为 dict[str, AgentConfig]

- [ ] **Step 1: 创建 `app/agent/models.py` — 模型构建**

```python
# app/agent/models.py
"""模型管理 — 从 DB 读取配置，构建 LangChain ChatModel"""

from __future__ import annotations

import logging
import time
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_openai import AzureChatOpenAI, ChatOpenAI

logger = logging.getLogger(__name__)

# --- TTL 缓存 ---
_CACHE_TTL = 300  # 5 分钟
_cache: dict[str, tuple[Any, float]] = {}


async def _get_model_info(model_id: str) -> dict[str, Any] | None:
    """从 DB 获取模型+供应商信息（TTL 缓存）"""
    now = time.monotonic()
    cached = _cache.get(model_id)
    if cached and cached[1] > now:
        return cached[0]

    from app.data.session import get_session
    from app.data.queries import get_model_info

    try:
        async with get_session() as session:
            result = await get_model_info(session, model_id)
    except Exception as e:
        logger.error("DB query error for model %s: %s", model_id, e)
        return None

    _cache[model_id] = (result, now + _CACHE_TTL)
    return result


class _ReasoningChatOpenAI(ChatOpenAI):
    """DeepSeek reasoning_content 保留 — 行为参考 agents/infra/model_builder.py"""
    # 完整实现从旧代码重写，保留 _create_chat_result / _normalize_content / _get_request_payload
    ...


_MODEL_BUILDERS: dict[str, type] = {
    "azure-http": AzureChatOpenAI,
    "deepseek": _ReasoningChatOpenAI,
    # google 和 openai-responses 也在这里注册
}


async def build_chat_model(model_id: str, **kwargs) -> BaseChatModel:
    """根据 model_id 构建 LangChain ChatModel

    Raises:
        ValueError: 模型未找到或配置不完整
    """
    info = await _get_model_info(model_id)
    if not info:
        raise ValueError(f"Model not found: {model_id}")

    client_type = info.get("client_type", "")

    # 根据 client_type 选择构建器
    # ... 行为参考 agents/infra/model_builder.py:188-325
    # 关键改动：用 _MODEL_BUILDERS dict 替代 if-elif 链
    ...
```

> **行为参考：** `app/agents/infra/model_builder.py` 完整实现 — _ReasoningChatOpenAI 的三个方法必须逐行对照。build_chat_model 的所有 client_type 分支必须覆盖。

- [ ] **Step 2: 创建 `app/agent/prompts.py` — Prompt 管理**

```python
# app/agent/prompts.py
"""Prompt 管理 — Langfuse prompt 拉取 + 变量注入"""

from __future__ import annotations

from langfuse import get_client as get_langfuse


def get_prompt(prompt_id: str):
    """从 Langfuse 获取 prompt（带本地缓存）"""
    return get_langfuse().get_prompt(prompt_id)


def compile_prompt(prompt_id: str, **variables) -> str:
    """获取 prompt 并编译变量"""
    prompt = get_prompt(prompt_id)
    return prompt.compile(**variables)
```

- [ ] **Step 3: 创建 `app/agent/tracing.py` — Trace 管理**

```python
# app/agent/tracing.py
"""Langfuse trace 管理"""

from __future__ import annotations

from typing import Any

from langfuse.langchain import CallbackHandler


def make_callback(
    *,
    trace_name: str | None = None,
    parent_run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构建 LangChain config with Langfuse callback"""
    cb_kwargs: dict[str, Any] = {}
    if parent_run_id:
        cb_kwargs["trace_id"] = parent_run_id
    if metadata:
        cb_kwargs["metadata"] = metadata

    config: dict[str, Any] = {"callbacks": [CallbackHandler(**cb_kwargs)]}
    if trace_name:
        config["run_name"] = trace_name
    return config
```

- [ ] **Step 4: 创建 `app/agent/core.py` — 统一 Agent**

```python
# app/agent/core.py
"""Agent — 统一思考入口

有 tools → LangGraph agent loop（多步推理）
没 tools → 单次 model.ainvoke()
response_model → model.with_structured_output()

重试逻辑只在这里写一次。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from langchain.agents import create_agent
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel

from app.agent.models import build_chat_model
from app.agent.prompts import compile_prompt, get_prompt
from app.agent.tracing import make_callback

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

RETRYABLE = (APITimeoutError, APIConnectionError, InternalServerError, RateLimitError)
_MAX_RETRIES = 2
_BACKOFF_BASE = 2
_BACKOFF_MAX = 8
_RECURSION_LIMIT = 12  # 12 步 ≈ 6 次工具调用


@dataclass(frozen=True)
class AgentConfig:
    """Agent 预设配置"""
    prompt_id: str
    model_id: str
    trace_name: str | None = None


# 预设 Agent 配置注册表
AGENTS: dict[str, AgentConfig] = {
    "main": AgentConfig("main", "main-chat-model", "main"),
    "research": AgentConfig("research_agent", "research-model", "research"),
    "schedule-ideation": AgentConfig("schedule_daily_ideation", "offline-model", "schedule-ideation"),
    "schedule-writer": AgentConfig("schedule_daily_writer", "offline-model", "schedule-writer"),
    "schedule-critic": AgentConfig("schedule_daily_critic", "offline-model", "schedule-critic"),
    "relationship-filter": AgentConfig("relationship_filter", "relationship-model", "relationship-filter"),
    "relationship-extract": AgentConfig("relationship_extract", "relationship-model", "relationship-extract"),
    "afterthought": AgentConfig("afterthought_conversation", "diary-model", "afterthought"),
    "voice-generator": AgentConfig("voice_generator", "offline-model", "voice-generator"),
    "dream-daily": AgentConfig("dream_daily", "diary-model", "dream-daily"),
    "dream-weekly": AgentConfig("dream_weekly", "diary-model", "dream-weekly"),
    "schedule-monthly": AgentConfig("schedule_monthly", "offline-model", "schedule-monthly"),
    "schedule-weekly": AgentConfig("schedule_weekly", "offline-model", "schedule-weekly"),
}


async def _retry(fn, *, max_retries: int = _MAX_RETRIES, label: str = ""):
    """统一重试逻辑"""
    for attempt in range(1, max_retries + 1):
        try:
            return await fn()
        except RETRYABLE as e:
            if attempt < max_retries:
                delay = min(_BACKOFF_BASE ** attempt, _BACKOFF_MAX)
                logger.warning("%s attempt %d/%d failed: %s, retry in %ds", label, attempt, max_retries, e, delay)
                await asyncio.sleep(delay)
            else:
                raise
    raise RuntimeError("Unreachable")


class Agent:
    """统一思考入口

    用法：
        # 无工具（等价于旧 LLMService.run）
        result = await Agent("afterthought").run(prompt_vars={...}, messages=[...])

        # 有工具（等价于旧 ChatAgent）
        async for chunk in Agent("main", tools=ALL_TOOLS).stream(
            prompt_vars={...}, messages=[...], context=ctx
        ):
            ...

        # 结构化输出（等价于旧 LLMService.extract）
        data = await Agent("relationship-filter").extract(
            prompt_vars={...}, messages=[...], response_model=FilterResult
        )

        # 临时覆盖 model_id
        result = await Agent("main", model_id="gpt-4o").run(...)
    """

    def __init__(
        self,
        name: str,
        *,
        tools: list | None = None,
        model_id: str | None = None,
        prompt_id: str | None = None,
        trace_name: str | None = None,
    ):
        config = AGENTS.get(name)
        self._prompt_id = prompt_id or (config.prompt_id if config else name)
        self._model_id = model_id or (config.model_id if config else "")
        self._trace_name = trace_name or (config.trace_name if config else name)
        self._tools = tools

    async def run(
        self,
        *,
        prompt_vars: dict[str, Any] | None = None,
        messages: list[BaseMessage | dict] | None = None,
        parent_config: RunnableConfig | None = None,
        context: Any = None,
    ) -> AIMessage:
        """同步执行，返回 AIMessage"""
        if self._tools:
            return await self._run_agentic(
                prompt_vars=prompt_vars or {},
                messages=messages or [],
                parent_config=parent_config,
                context=context,
            )
        return await self._run_direct(
            prompt_vars=prompt_vars or {},
            messages=messages or [],
            parent_config=parent_config,
        )

    async def stream(
        self,
        *,
        prompt_vars: dict[str, Any] | None = None,
        messages: list[BaseMessage | dict] | None = None,
        parent_config: RunnableConfig | None = None,
        context: Any = None,
    ) -> AsyncGenerator[AIMessageChunk | ToolMessage, None]:
        """流式执行"""
        if self._tools:
            async for chunk in self._stream_agentic(
                prompt_vars=prompt_vars or {},
                messages=messages or [],
                parent_config=parent_config,
                context=context,
            ):
                yield chunk
        else:
            async for chunk in self._stream_direct(
                prompt_vars=prompt_vars or {},
                messages=messages or [],
                parent_config=parent_config,
            ):
                yield chunk

    async def extract(
        self,
        response_model: type[T],
        *,
        prompt_vars: dict[str, Any] | None = None,
        messages: list[BaseMessage | dict] | None = None,
        parent_config: RunnableConfig | None = None,
    ) -> T:
        """结构化提取，返回 Pydantic model"""
        model = await build_chat_model(self._model_id)
        structured = model.with_structured_output(response_model)

        full_messages = self._build_messages(prompt_vars or {}, messages or [])
        config = self._make_config(parent_config)

        return await _retry(
            lambda: structured.ainvoke(full_messages, config=config),
            label=f"extract({self._trace_name})",
        )

    # --- 内部方法 ---

    def _build_messages(
        self, prompt_vars: dict, messages: list
    ) -> list[BaseMessage | dict]:
        """拼接 system prompt + messages"""
        system = compile_prompt(
            self._prompt_id,
            currDate=datetime.now().strftime("%Y-%m-%d"),
            currTime=datetime.now().strftime("%H:%M:%S"),
            **prompt_vars,
        )
        return [SystemMessage(content=system), *messages]

    def _make_config(self, parent_config: RunnableConfig | None = None) -> dict:
        """构建运行配置"""
        if parent_config:
            config = dict(parent_config)
            if self._trace_name:
                config["run_name"] = self._trace_name
            return config
        config = make_callback(trace_name=self._trace_name)
        config.setdefault("recursion_limit", _RECURSION_LIMIT)
        return config

    async def _run_direct(self, prompt_vars, messages, parent_config) -> AIMessage:
        """无工具：单次调用"""
        model = await build_chat_model(self._model_id)
        full_messages = self._build_messages(prompt_vars, messages)
        config = self._make_config(parent_config)
        return await _retry(
            lambda: model.ainvoke(full_messages, config=config),
            label=f"run({self._trace_name})",
        )

    async def _stream_direct(self, prompt_vars, messages, parent_config):
        """无工具：流式"""
        model = await build_chat_model(self._model_id)
        full_messages = self._build_messages(prompt_vars, messages)
        config = self._make_config(parent_config)
        async for chunk in model.astream(full_messages, config=config):
            yield chunk

    async def _run_agentic(self, prompt_vars, messages, parent_config, context) -> AIMessage:
        """有工具：LangGraph agent loop"""
        langfuse_prompt = get_prompt(self._prompt_id)
        model = await build_chat_model(self._model_id)
        prompt = langfuse_prompt.get_langchain_prompt(
            currDate=datetime.now().strftime("%Y-%m-%d"),
            currTime=datetime.now().strftime("%H:%M:%S"),
            **prompt_vars,
        )
        agent = create_agent(model, self._tools, system_prompt=prompt, context_schema=type(context) if context else None)
        config = self._make_config(parent_config)

        result = await _retry(
            lambda: agent.ainvoke({"messages": messages}, context=context, config=config),
            label=f"run_agentic({self._trace_name})",
        )
        return result["messages"][-1]

    async def _stream_agentic(self, prompt_vars, messages, parent_config, context):
        """有工具：LangGraph agent loop 流式"""
        langfuse_prompt = get_prompt(self._prompt_id)
        model = await build_chat_model(self._model_id)
        prompt = langfuse_prompt.get_langchain_prompt(
            currDate=datetime.now().strftime("%Y-%m-%d"),
            currTime=datetime.now().strftime("%H:%M:%S"),
            **prompt_vars,
        )
        agent = create_agent(model, self._tools, system_prompt=prompt, context_schema=type(context) if context else None)
        config = self._make_config(parent_config)

        for attempt in range(1, _MAX_RETRIES + 1):
            tokens_yielded = False
            try:
                async for token, _ in agent.astream(
                    {"messages": messages},
                    context=context,
                    stream_mode="messages",
                    config=config,
                ):
                    tokens_yielded = True
                    yield token
                return
            except RETRYABLE as e:
                if tokens_yielded:
                    raise  # 已输出 token 则不重试
                if attempt < _MAX_RETRIES:
                    delay = min(_BACKOFF_BASE ** attempt, _BACKOFF_MAX)
                    logger.warning("stream_agentic attempt %d failed: %s, retry in %ds", attempt, e, delay)
                    await asyncio.sleep(delay)
                else:
                    raise
```

- [ ] **Step 5: 创建 `app/agent/__init__.py`**

```python
# app/agent/__init__.py
"""Agent — 统一思考入口"""
from app.agent.core import Agent, AgentConfig, AGENTS
```

- [ ] **Step 6: 写测试**

```python
# tests/unit/agent/test_core.py
"""Agent 核心测试 — 验证统一接口"""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.core import Agent, AGENTS, AgentConfig


@pytest.mark.unit
class TestAgentConfig:
    def test_all_agents_registered(self):
        """所有预设 agent 都已注册"""
        expected = {
            "main", "research",
            "schedule-ideation", "schedule-writer", "schedule-critic",
            "relationship-filter", "relationship-extract",
            "afterthought", "voice-generator",
            "dream-daily", "dream-weekly",
            "schedule-monthly", "schedule-weekly",
        }
        assert set(AGENTS.keys()) == expected

    def test_agent_config_has_required_fields(self):
        for name, config in AGENTS.items():
            assert config.prompt_id, f"{name} missing prompt_id"
            assert config.model_id, f"{name} missing model_id"


@pytest.mark.unit
class TestAgentDirect:
    """无工具调用（等价于旧 LLMService）"""

    @patch("app.agent.core.build_chat_model")
    @patch("app.agent.core.compile_prompt", return_value="You are helpful.")
    async def test_run_returns_ai_message(self, mock_prompt, mock_build):
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = AIMessage(content="hello")
        mock_build.return_value = mock_model

        agent = Agent("afterthought")
        result = await agent.run(
            prompt_vars={"persona_name": "赤尾"},
            messages=[HumanMessage(content="生成碎片")],
        )
        assert isinstance(result, AIMessage)
        assert result.content == "hello"
        mock_model.ainvoke.assert_called_once()

    @patch("app.agent.core.build_chat_model")
    @patch("app.agent.core.compile_prompt", return_value="You are helpful.")
    async def test_extract_returns_pydantic_model(self, mock_prompt, mock_build):
        from pydantic import BaseModel

        class TestOutput(BaseModel):
            interesting: bool
            reason: str

        mock_structured = AsyncMock()
        mock_structured.ainvoke.return_value = TestOutput(interesting=True, reason="test")
        mock_model = MagicMock()
        mock_model.with_structured_output.return_value = mock_structured
        mock_build.return_value = mock_model

        agent = Agent("relationship-filter")
        result = await agent.extract(
            TestOutput,
            prompt_vars={},
            messages=[HumanMessage(content="filter")],
        )
        assert isinstance(result, TestOutput)
        assert result.interesting is True


@pytest.mark.unit
class TestAgentWithTools:
    """有工具调用（等价于旧 ChatAgent）"""

    @patch("app.agent.core.build_chat_model")
    @patch("app.agent.core.get_prompt")
    @patch("app.agent.core.create_agent")
    async def test_run_agentic_returns_last_message(self, mock_create, mock_prompt, mock_build):
        mock_agent = AsyncMock()
        mock_agent.ainvoke.return_value = {
            "messages": [AIMessage(content="thinking..."), AIMessage(content="final answer")]
        }
        mock_create.return_value = mock_agent
        mock_build.return_value = MagicMock()
        mock_prompt.return_value = MagicMock()
        mock_prompt.return_value.get_langchain_prompt.return_value = "system prompt"

        dummy_tool = MagicMock()
        agent = Agent("main", tools=[dummy_tool])
        result = await agent.run(
            prompt_vars={"identity": "赤尾"},
            messages=[HumanMessage(content="你好")],
        )
        assert result.content == "final answer"


@pytest.mark.unit
class TestAgentOverrides:
    """临时覆盖参数"""

    def test_model_id_override(self):
        agent = Agent("main", model_id="gpt-4o")
        assert agent._model_id == "gpt-4o"

    def test_unknown_name_uses_name_as_prompt_id(self):
        agent = Agent("custom-agent")
        assert agent._prompt_id == "custom-agent"
```

- [ ] **Step 7: 运行测试 + ruff**

```bash
uv run ruff check app/agent/ --fix && uv run ruff format app/agent/
uv run pytest tests/unit/agent/ -v
```

- [ ] **Step 8: Commit**

```bash
git add app/agent/ tests/unit/agent/
git commit -m "refactor(agent-service): 新建 agent/ 模块 — 统一 Agent 抽象（ChatAgent + LLMService → Agent）"
```

---

### Task 4: `agent/embedding.py` — Embedding（例外路径）

- [ ] **Step 1: 创建 `app/agent/embedding.py`**

```python
# app/agent/embedding.py
"""Embedding — 不走 Agent 的例外路径"""

from __future__ import annotations

from dataclasses import dataclass

from app.agent.models import build_chat_model


@dataclass
class HybridEmbedding:
    dense: list[float]
    sparse: dict[int, float]


async def embed_text(model_id: str, text: str, *, instruction: str = "") -> list[float]:
    """文本 embedding"""
    # 行为参考：agents/clients/base.py + ark_client.py embed()
    ...

async def embed_hybrid(model_id: str, text: str, *, instruction: str = "") -> HybridEmbedding:
    """混合 embedding（dense + sparse）"""
    # 行为参考：agents/clients/ark_client.py embed_hybrid()
    ...
```

> **行为参考：** `app/agents/clients/ark_client.py` embed/embed_hybrid + `app/agents/infra/embedding/`

- [ ] **Step 2: 测试 + commit**

```bash
git add app/agent/embedding.py tests/unit/agent/test_embedding.py
git commit -m "refactor(agent-service): agent/embedding — embedding 独立路径"
```

---

### Task 5: `agent/tools/` — 工具集

从 `agents/tools/` 重写。每个工具文件独立，共享 error handling 装饰器。

**Files:**
- Create: `app/agent/tools/__init__.py`
- Create: `app/agent/tools/search.py` (合并 web + image + reranker + models)
- Create: `app/agent/tools/history.py` (合并 chat_history + search + members)
- Create: `app/agent/tools/image.py` (合并 generate + read + processor)
- Create: `app/agent/tools/recall.py`
- Create: `app/agent/tools/delegation.py`
- Create: `app/agent/tools/sandbox.py`
- Create: `app/agent/tools/skill.py`
- Test: `tests/unit/agent/tools/test_search.py` 等

**关键改动：**
- `search/models.py` 550 行 → 精简数据模型，去除未使用字段
- `search/bangumi.py` 491 行 → 移到 skills/ 下（它是 bangumi 技能的实现，不是通用搜索工具）
- 所有工具返回 `str`，不返回 `list[dict]`（工具结果应该是文本描述）
- `@tool` 装饰器 + 统一错误处理

> **注意：** 每个工具的行为必须逐行对照旧实现。工具是 agent 的手，搬错了整个系统就瘫了。

- [ ] **Step 1-8: 逐个工具文件重写 + 测试**

每个工具文件按 TDD 流程：写测试 → 验证失败 → 实现 → 验证通过 → commit。

> **行为参考：** `app/agents/tools/` 目录下所有文件

- [ ] **Step 9: Commit**

```bash
git add app/agent/tools/ tests/unit/agent/tools/
git commit -m "refactor(agent-service): agent/tools — 工具集重写 + bangumi 移到 skills"
```

---

## Phase C: 领域模块

### Task 6: `memory/` — 记忆与身份

将 `services/{afterthought,identity_drift,voice_generator,relationship_memory,memory_context,debounced_pipeline,timeline_formatter}.py` 合并为 `app/memory/`。

**Files:**
- Create: `app/memory/__init__.py`
- Create: `app/memory/debounce.py` (from `services/debounced_pipeline.py`)
- Create: `app/memory/afterthought.py`
- Create: `app/memory/drift.py`
- Create: `app/memory/voice.py`
- Create: `app/memory/relationships.py`
- Create: `app/memory/dreams.py` (业务逻辑从 `workers/dream_worker.py` 下沉)
- Create: `app/memory/context.py`
- Test: `tests/unit/memory/test_afterthought.py` 等

**关键改动：**
- 所有 LLM 调用改为 `Agent("afterthought").run(...)` 或 `Agent(...).extract(response_model=...)`
- 消灭 `_extract_text()`（Agent.extract 自动解析）
- timeline 格式化逻辑内聚为模块内部函数，不单独成文件
- 手写单例 `get_instance()` → 模块级实例

```python
# app/memory/afterthought.py (示例)
"""事后回想 — debounce → 生成经历碎片"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from langchain_core.messages import HumanMessage

from app.agent import Agent
from app.data.session import get_session
from app.data import queries
from app.memory.debounce import DebouncedPipeline

logger = logging.getLogger(__name__)
CST = timezone(timedelta(hours=8))


class _Afterthought(DebouncedPipeline):
    def __init__(self):
        super().__init__(debounce_seconds=300, max_buffer=15)

    async def process(self, chat_id: str, persona_id: str, event_count: int) -> None:
        await _generate_fragment(chat_id, persona_id)


async def _generate_fragment(chat_id: str, persona_id: str) -> None:
    now = datetime.now(CST)
    start_ts = int((now - timedelta(hours=2)).timestamp() * 1000)
    end_ts = int(now.timestamp() * 1000)

    async with get_session() as session:
        messages = await queries.get_chat_messages_in_range(session, chat_id, start_ts, end_ts)
        if not messages:
            return

        persona = await queries.get_persona(session, persona_id)
        if not persona:
            return

    scene = await _build_scene(chat_id, messages)
    timeline = _format_timeline(messages, persona.display_name)

    # 统一走 Agent — 不再裸调 LLMService
    result = await Agent("afterthought").run(
        prompt_vars={
            "persona_name": persona.display_name,
            "persona_lite": persona.persona_lite,
            "scene": scene,
            "messages": timeline,
        },
        messages=[HumanMessage(content="生成经历碎片")],
    )

    content = result.content if isinstance(result.content, str) else ""
    if not content.strip():
        return

    async with get_session() as session:
        await queries.create_fragment(session, ...)

    # 关系记忆提取（fire-and-forget）
    ...


# 模块级实例
afterthought = _Afterthought()
```

- [ ] **Step 1-14: 逐个文件 TDD 重写**

每个文件：写测试 → 实现 → 通过 → commit。

> **行为参考：** `app/services/` 对应的每个旧文件

- [ ] **Step 15: Commit**

```bash
git add app/memory/ tests/unit/memory/
git commit -m "refactor(agent-service): 新建 memory/ 模块 — 记忆与身份"
```

---

### Task 7: `life/` — 自主生活

将 `services/{life_engine,glimpse,schedule_context}.py` + `workers/{schedule_worker,proactive_scanner}.py` 的业务逻辑合并为 `app/life/`。

**Files:**
- Create: `app/life/__init__.py`
- Create: `app/life/engine.py` (from `services/life_engine.py`)
- Create: `app/life/schedule.py` (业务逻辑从 `workers/schedule_worker.py` 下沉)
- Create: `app/life/glimpse.py` (from `services/glimpse.py`)
- Create: `app/life/proactive.py` (from `workers/proactive_scanner.py`)
- Test: `tests/unit/life/test_engine.py` 等

**关键改动：**
- schedule 的 Ideation→Writer→Critic Agent 管线从 worker 下沉到这里
- GlimpseResult 用 Enum
- Life Engine 解析失败返回旧状态（不丢字段）
- 所有 LLM 调用走 Agent

- [ ] **Step 1-10: 逐个文件 TDD 重写**

> **行为参考：**
> - `app/services/life_engine.py`
> - `app/services/glimpse.py`
> - `app/workers/schedule_worker.py`（500 行 → 下沉到 life/schedule.py）
> - `app/workers/proactive_scanner.py`
> - `app/services/schedule_context.py`

- [ ] **Step 11: Commit**

```bash
git add app/life/ tests/unit/life/
git commit -m "refactor(agent-service): 新建 life/ 模块 — 自主生活"
```

---

### Task 8: `chat/` — 对话管线

将 `agents/domains/main/` + `agents/graphs/` + 散落的 services 合并为 `app/chat/`。

**Files:**
- Create: `app/chat/__init__.py`
- Create: `app/chat/pipeline.py` (from `agents/domains/main/agent.py`)
- Create: `app/chat/context.py` (from `agents/domains/main/context_builder.py` + `services/bot_context.py`)
- Create: `app/chat/stream.py` (from `agents/domains/main/stream_handler.py` + `safety_race.py`)
- Create: `app/chat/safety.py` (from `agents/graphs/pre/` + `agents/graphs/post/`)
- Create: `app/chat/post_actions.py` (from `agents/domains/main/post_actions.py`)
- Create: `app/chat/router.py` (from `services/message_router.py`)
- Test: `tests/unit/chat/test_pipeline.py` 等

**关键改动：**
- `stream_chat` 入口签名不变
- 内部用 `Agent("main", tools=TOOLS)` 替代直接构造 ChatAgent
- safety 作为 chat 的子模块，不独立
- BotContext 简化：统一工厂方法，去掉三种 init 路径

```python
# app/chat/pipeline.py (骨架)
"""对话管线 — stream_chat 主编排"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator

from langfuse import get_client as get_langfuse, propagate_attributes

from app.agent import Agent
from app.agent.tools import ALL_TOOLS  # 在 agent/tools/__init__.py 中定义
from app.chat.context import build_chat_context
from app.chat.post_actions import schedule_post_actions
from app.chat.safety import run_pre_check
from app.chat.stream import StreamState, handle_token, buffer_until_pre

logger = logging.getLogger(__name__)

async def stream_chat(
    message_id: str,
    session_id: str | None = None,
    persona_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """主聊天流式响应入口"""
    # 行为参考：agents/domains/main/agent.py
    # 关键改动：ChatAgent → Agent("main", tools=ALL_TOOLS)
    ...
```

- [ ] **Step 1-16: 逐个文件 TDD 重写**

> **行为参考：**
> - `app/agents/domains/main/agent.py`
> - `app/agents/domains/main/context_builder.py`
> - `app/agents/domains/main/stream_handler.py`
> - `app/agents/domains/main/safety_race.py`
> - `app/agents/domains/main/post_actions.py`
> - `app/agents/graphs/pre/`
> - `app/agents/graphs/post/`
> - `app/services/bot_context.py`
> - `app/services/content_parser.py`
> - `app/services/message_router.py`
> - `app/services/banned_word.py`
> - `app/services/download_permission.py`

- [ ] **Step 17: Commit**

```bash
git add app/chat/ tests/unit/chat/
git commit -m "refactor(agent-service): 新建 chat/ 模块 — 对话管线"
```

---

## Phase D: 编排层

### Task 9: `workers/` — 执行基底（薄层重写）

**原则：** worker 只编排，不做业务。所有业务逻辑已在 Phase C 下沉到领域模块。

**Files:**
- Create: `app/workers/__init__.py`
- Create: `app/workers/common.py` (persona 批处理 + 错误处理装饰器)
- Create: `app/workers/cron.py` (所有 cron 任务定义)
- Create: `app/workers/arq_settings.py` (ARQ 配置，from `unified_worker.py`)
- Create: `app/workers/chat_consumer.py` (MQ 消费者)
- Create: `app/workers/post_consumer.py`
- Create: `app/workers/vectorize.py`
- Test: `tests/unit/workers/test_common.py`

**关键改动：**
- `for_each_persona()` 高阶函数消灭 5 处重复
- cron 函数全部变成 3-5 行的薄包装
- schedule_worker.py 的 500 行 → cron.py 中 ~20 行（调用 `life.schedule.generate_schedule()`）
- 错误处理装饰器保留，合并为 common.py

```python
# app/workers/common.py
"""Worker 共享工具"""

from __future__ import annotations

import logging
from collections.abc import Callable, Awaitable

from app.data.session import get_session
from app.data.queries import get_all_persona_ids

logger = logging.getLogger(__name__)


async def for_each_persona(
    fn: Callable[[str], Awaitable[None]],
    *,
    label: str = "",
) -> None:
    """遍历所有 persona 执行函数，统一错误处理"""
    async with get_session() as session:
        persona_ids = await get_all_persona_ids(session)

    for persona_id in persona_ids:
        try:
            await fn(persona_id)
        except Exception:
            logger.exception("[%s] %s failed", persona_id, label)
```

```python
# app/workers/cron.py (示例)
"""Cron 任务定义 — 全部是薄包装"""

from app.workers.common import for_each_persona


async def cron_generate_voice(ctx) -> None:
    from app.memory.voice import generate_voice
    await for_each_persona(generate_voice, label="voice")


async def cron_generate_dreams(ctx) -> None:
    from app.memory.dreams import compress_daily
    await for_each_persona(compress_daily, label="dream-daily")


async def cron_life_engine_tick(ctx) -> None:
    from app.life.engine import tick
    await for_each_persona(tick, label="life-tick")


async def cron_glimpse(ctx) -> None:
    from app.life.glimpse import run_glimpse
    await for_each_persona(run_glimpse, label="glimpse")


async def cron_generate_daily_plan(ctx) -> None:
    from app.life.schedule import generate_daily
    await for_each_persona(generate_daily, label="schedule-daily")

# ... 其他 cron 任务同理
```

- [ ] **Step 1-8: TDD 重写**

- [ ] **Step 9: Commit**

```bash
git add app/workers/ tests/unit/workers/
git commit -m "refactor(agent-service): workers/ 薄层重写 — for_each_persona + cron 瘦身"
```

---

### Task 10: `api/` — HTTP 路由

**Files:**
- Create: `app/api/__init__.py`
- Create: `app/api/routes.py` (合并 `api/router.py` + `api/schedule.py`)
- Create: `app/api/middleware.py` (from `middleware/` + `utils/middlewares/`)
- Test: `tests/unit/api/test_routes.py`

- [ ] **Step 1-4: 重写 + 测试 + commit**

```bash
git add app/api/ tests/unit/api/
git commit -m "refactor(agent-service): api/ 路由 + 中间件合并"
```

---

### Task 10.5: `app/main.py` — 入口更新

更新 FastAPI app 入口，指向新模块。

- [ ] **Step 1: 重写 `app/main.py`**

```python
# app/main.py
"""FastAPI 应用入口"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.middleware import HeaderContextMiddleware, PrometheusMiddleware
from app.api.routes import router
from app.infra.config import settings

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.infra.qdrant import init_collections
    await init_collections()

    from app.skills.registry import SkillRegistry
    from pathlib import Path
    SkillRegistry.load_all(Path(__file__).parent / "skills" / "definitions")

    consumer_tasks: list[asyncio.Task] = []
    if settings.rabbitmq_url:
        from app.workers.chat_consumer import start_chat_consumer
        from app.workers.post_consumer import start_post_consumer
        consumer_tasks.append(asyncio.create_task(start_post_consumer()))
        consumer_tasks.append(asyncio.create_task(start_chat_consumer()))

    yield

    for task in consumer_tasks:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    if settings.rabbitmq_url:
        from app.infra.rabbitmq import mq
        await mq.close()

app = FastAPI(lifespan=lifespan)
app.add_middleware(PrometheusMiddleware)
app.add_middleware(HeaderContextMiddleware)
app.include_router(router)
```

- [ ] **Step 2: Commit**

```bash
git add app/main.py
git commit -m "refactor(agent-service): main.py 指向新模块"
```

---

## Phase E: 清扫

### Task 11: 删旧代码 + 验收

- [ ] **Step 1: 删除旧目录**

```bash
rm -rf app/agents/ app/services/ app/orm/ app/clients/ app/config/ app/types/ app/middleware/ app/utils/
```

- [ ] **Step 2: 删除 long_tasks/（确认无引用后）**

```bash
# 先确认
grep -r "long_tasks" app/ --include="*.py"
# 如果只有 workers/arq_settings.py 引用了 poll_and_execute_tasks，决定保留或迁移
```

- [ ] **Step 3: grep 零残留验证**

```bash
# 旧 import 路径必须为零
grep -r "from app\.agents\." app/ --include="*.py" | grep -v __pycache__
grep -r "from app\.services\." app/ --include="*.py" | grep -v __pycache__
grep -r "from app\.orm\." app/ --include="*.py" | grep -v __pycache__
grep -r "from app\.clients\." app/ --include="*.py" | grep -v __pycache__
grep -r "from app\.config\." app/ --include="*.py" | grep -v __pycache__
grep -r "from app\.types\." app/ --include="*.py" | grep -v __pycache__
grep -r "from app\.middleware\." app/ --include="*.py" | grep -v __pycache__
grep -r "from app\.utils\." app/ --include="*.py" | grep -v __pycache__

# 旧类名搜不到
grep -r "ChatAgent" app/ --include="*.py" | grep -v __pycache__
grep -r "LLMService" app/ --include="*.py" | grep -v __pycache__
grep -r "get_instance" app/ --include="*.py" | grep -v __pycache__
grep -r "_extract_text" app/ --include="*.py" | grep -v __pycache__
```

所有搜索结果必须为空。

- [ ] **Step 4: ruff 全量检查**

```bash
uv run ruff check app/ --fix
uv run ruff format app/
# 必须零报错
uv run ruff check app/
```

- [ ] **Step 5: 全量测试**

```bash
uv run pytest tests/ -v
```

- [ ] **Step 6: 代码量验收**

```bash
find app/ -name "*.py" ! -path "*/test*" ! -path "*__pycache__*" | xargs wc -l | tail -1
# 目标：≤ 10,800 行
```

- [ ] **Step 7: 模块数量验收**

```bash
ls -d app/*/
# 应该是 9 个：agent/ chat/ memory/ life/ data/ infra/ workers/ skills/ api/
# 加上 long_tasks/（如果保留）
```

- [ ] **Step 8: 死代码检查**

```bash
# 定义了但没被 import 的文件
# 用 pyright 或手动 grep 验证每个 .py 文件都被某个 import 引用
```

- [ ] **Step 9: Final commit**

```bash
git add -A
git commit -m "refactor(agent-service): 删除旧代码 + 验收通过"
```

---

## 验收清单（总览）

| # | 指标 | 验证方式 |
|---|------|---------|
| 1 | 非 test 代码量 ≤ 10,800 行 | `find + wc -l` |
| 2 | 新测试全部通过 | `pytest tests/ -v` |
| 3 | grep 零残留 | Step 3 的所有 grep 命令 |
| 4 | 无重复定义 | `_extract_text` 等关键模式搜索为零 |
| 5 | 无死代码 | pyright + grep |
| 6 | 9 个顶层模块 | `ls -d app/*/` |
| 7 | ruff 零报错 | `ruff check app/` |
| 8 | 全部 Pythonic | Code review |
| 9 | 质疑已知问题 | 每个 Task 的实现过程中标记并讨论 |
