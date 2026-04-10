# agent-service 架构重构设计

## 背景

agent-service 约 19,400 行 Python 代码，174 个文件。经过多轮功能迭代（context 架构、关系记忆、多 bot、Life Engine），积累了大量技术债：

- **LLM 调用 4 种方式共存**：ChatAgent / ModelBuilder+手动 ainvoke / 裸 structured_output / embedding client，无统一入口
- **消息时间线格式化 4 套重复实现**：relationship_memory、glimpse、identity_drift、afterthought 各写一遍
- **两阶段防抖管线代码雷同**：identity_drift 和 afterthought 的 Manager 类结构几乎一致
- **Persona 上下文加载 5 处重复**：每个 service 自己 `get_bot_persona() → 取 display_name / persona_lite`
- **CRUD God Object**：orm/crud.py 316 行混了 4 种实体的操作和业务逻辑
- **14+ 处裸 SQL**：services 和 workers 直接用 AsyncSessionLocal 绕过 CRUD 层
- **主 agent 编排器 464 行**：混了安全竞速、流式输出、后处理任务触发
- **Worker 无公共错误处理**：11 个 worker 各自为政
- **其他去重项**：banned_word 双重实现、图片注册 3 处重复、tool_error_handler 覆盖不全

## 目标

- 建立可复用的抽象层，让"加新管线"变成填模板而非从头写
- 职责清晰：每一层只做该做的事，不越界
- 每个 commit 闭环且可编译通过测试
- 允许顺手改进（补 trace、补重试、补 error handler）

## 不做的事

- 不做大规模目录重组
- 不改对外 API 接口
- 不改 MQ 消息格式
- 不改数据库 schema

## 约束

- 分支：`refactor/agent-service-architecture`
- 一个 PR 合入 main，按 commit 分阶段
- TDD：每个 commit 先写测试再写实现
- 每个 commit 编译通过 + 测试通过

---

## Phase 1：基础设施层

解决最底层的重复问题，后续所有阶段依赖此处建的抽象。

### 1.1 LLMService — 统一 LLM 调用入口

**位置**：`app/agents/infra/llm_service.py`

**接口**：

```python
class LLMService:
    """所有 LLM 调用的唯一入口。自带 Langfuse trace + 重试。"""

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
    ) -> AIMessage:
        """非流式调用，返回完整结果"""

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
    ) -> AsyncGenerator[AIMessageChunk, None]:
        """流式调用"""

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
    ) -> BaseModel:
        """结构化输出，替代 model.with_structured_output().ainvoke()"""
```

**内部实现**：复用现有 ChatAgent + ModelBuilder，不重写。ChatAgent 降级为 LLMService 内部实现细节，不再被外部直接使用。

**迁移范围**：

| 现有调用方 | 当前方式 | 迁移到 |
|-----------|---------|--------|
| life_engine.py | ModelBuilder + 手动 ainvoke | `LLMService.run` |
| glimpse.py | ModelBuilder + 手动 ainvoke | `LLMService.run` |
| afterthought.py | ChatAgent.run | `LLMService.run` |
| voice_generator.py | ChatAgent.run | `LLMService.run` |
| relationship_memory.py | ChatAgent.run x2 | `LLMService.run` / `LLMService.extract` |
| dream_worker.py | ChatAgent.run | `LLMService.run` |
| schedule_worker.py | ChatAgent + ModelBuilder 混用 | `LLMService.run` |
| 5 个 safety/complexity 节点 | 裸 structured_output | `LLMService.extract` |

### 1.2 PersonaLoader — 统一 Persona 上下文加载

**位置**：`app/services/persona_loader.py`

**接口**：

```python
@dataclass(frozen=True)
class PersonaContext:
    persona_id: str
    display_name: str
    persona_lite: str
    bot_name: str | None = None

async def load_persona(persona_id: str) -> PersonaContext:
    """加载 persona 上下文，带进程内 TTL 缓存"""
```

**迁移范围**：identity_drift、glimpse、afterthought、voice_generator、relationship_memory 中 5 处 inline 的 persona 加载逻辑。

---

## Phase 2：管线层

### 2.1 TimelineFormatter — 统一消息时间线格式化

**位置**：`app/services/timeline_formatter.py`

**接口**：

```python
async def format_timeline(
    messages: list[ConversationMessage],
    persona_name: str,
    *,
    tz: ZoneInfo | None = None,
    max_messages: int | None = None,
    with_ids: bool = False,
    username_resolver: Callable | None = None,
) -> str:
    """统一的消息时间线格式化"""
```

**迁移范围**：

| 现有实现 | 位置 | 处理方式 |
|---------|------|---------|
| `format_timeline()` | relationship_memory.py | 删除，迁移到新模块 |
| `_format_messages()` | glimpse.py | 删除 |
| `_get_recent_messages()` | identity_drift.py | 删除 |
| afterthought.py import | 从 relationship_memory 引 | 改为 import 新模块 |

注意：`context_builder.py` 的历史消息构建不动——那是构建 LangChain Message 对象给主 agent 用的，与"格式化成文本给 prompt 塞变量"是不同职责。

### 2.2 DebouncedPipeline — 防抖管线基类

**位置**：`app/services/debounced_pipeline.py`

**接口**：

```python
class DebouncedPipeline(ABC):
    """两阶段防抖管线：收集事件 → 防抖等待 → 批量处理"""

    def __init__(self, debounce_seconds: float, max_buffer: int): ...

    async def on_event(self, chat_id: str, persona_id: str, event: Any) -> None:
        """收到事件，加入 buffer，重置防抖计时器"""

    @abstractmethod
    async def process(self, chat_id: str, persona_id: str, events: list) -> None:
        """子类实现具体的批量处理逻辑"""
```

**迁移范围**：

- `AfterthoughtManager` → 继承 `DebouncedPipeline`，只实现 `process`
- `IdentityDriftManager` → 同上

### 2.3 ImageProcessor — 统一图片上传注册

**位置**：`app/agents/tools/image/processor.py`

**接口**：

```python
class ImageProcessor:
    @staticmethod
    async def upload_and_register(
        url: str,
        registry: ImageRegistry | None = None,
    ) -> str:
        """下载 → TOS 上传 → 注册，返回可用 URL"""
```

**迁移范围**：context_builder.py、search/image.py、image/generate.py 中 3 处图片上传注册逻辑。

---

## Phase 3：数据层

### 3.1 拆分 CRUD God Object

**目标结构**：

```
orm/
  crud/
    __init__.py          # re-export，保持外部 import 兼容
    persona.py           # get_bot_persona, get_gray_config, resolve_persona_id, resolve_bot_name
    model_provider.py    # get_model_and_provider_info, parse_model_id
    message.py           # get_message_content, get_chat_messages_in_range, get_username, get_group_name
    schedule.py          # get_plan_for_period, upsert_schedule 等
    life_engine.py       # load_state, save_state, load_glimpse_state
  memory_crud.py         # 已独立，不动
  models.py              # 不动
  memory_models.py       # 不动
  base.py                # 不动
```

`crud/__init__.py` re-export 所有公开函数，现有 `from app.orm.crud import xxx` 无需改动。

### 3.2 收敛裸 SQL 到 CRUD 层

**规则**：services/ 和 workers/ 不允许直接 import `AsyncSessionLocal`。所有数据库访问通过 `orm/crud/` 下的函数。

**迁移清单**：

| 现有位置 | 做的事 | 迁移到 |
|---------|-------|--------|
| life_engine.py 直接 session | 读写 life_engine_state | `crud/life_engine.py` |
| bot_context.py `_resolve_persona_id` | raw SQL 查 persona | `crud/persona.py` |
| bot_context.py `_resolve_bot_name_for_persona` | raw SQL 查 bot_name | `crud/persona.py` |
| message_router.py 裸 SQL | 查 persona 路由 | `crud/persona.py` |
| quick_search.py 复杂 join | 消息搜索 | `crud/message.py` |
| chat_consumer.py raw SQL UPDATE | 更新消息状态 | `crud/message.py` |
| post_consumer.py raw SQL UPDATE | 更新 safety_status | `crud/message.py` |
| vectorize_worker.py 直接 session | 读写向量化状态 | `crud/message.py` |
| glimpse_worker.py 直接 session | 读 glimpse_state | `crud/life_engine.py` |
| afterthought.py 查 group_name | 内联 SQL | `crud/message.py` |
| glimpse.py 查 group_name | 内联 SQL | `crud/message.py` |
| download_permission.py | 权限查询 | `crud/persona.py` |

### 3.3 业务逻辑移出 ORM

`parse_model_id()`（字符串解析工具函数）和 `get_model_and_provider_info()` 中的 fallback 选择逻辑属于业务规则，移到 `agents/infra/model_builder.py`。`crud/model_provider.py` 只做纯数据查询。

---

## Phase 4：编排层

### 4.1 拆分主 Agent 编排器

**现有**：`agents/domains/main/agent.py` 464 行

**目标结构**：

```
agents/domains/main/
  agent.py              # 瘦编排器：串流程
  safety_race.py        # pre-safety 与主 agent 的竞速逻辑
  stream_handler.py     # 流式输出处理（AIMessageChunk/ToolMessage/SPLIT_MARKER）
  post_actions.py       # fire-and-forget 后处理任务
```

`agent.py` 变成：

```python
async def stream_chat(context: AgentContext) -> AsyncGenerator:
    # 1. 启动 pre-safety（竞速）       → safety_race
    # 2. 构建 context                   → context_builder（已有）
    # 3. 主 agent stream               → stream_handler
    # 4. 触发后处理                     → post_actions
```

### 4.2 统一 Worker 错误处理

**位置**：`app/workers/error_handling.py`

**接口**：

```python
def mq_error_handler(publish_error: bool = True):
    """MQ consumer 的错误处理装饰器：log + 可选 publish error message"""

def cron_error_handler():
    """arq cron job 的错误处理装饰器：log + Langfuse event + 不中断调度"""
```

不搞 base class（MQ consumer 和 arq cron 生命周期差异太大），用装饰器统一错误处理契约。

### 4.3 去重收尾

| 问题 | 处理方式 |
|------|---------|
| banned_word 在 pre/post safety 各实现一次 | 提取到 `agents/graphs/shared/banned_word.py`，两边引用 |
| tool_error_handler 只覆盖 4/10 工具 | 补齐到所有工具 |
| content_parser.py 在 utils/ 但是业务逻辑 | 移到 services/ 或并入 TimelineFormatter 依赖链 |

---

## 依赖关系

```
Phase 1（基础设施）→ Phase 2（管线）→ Phase 3（数据）→ Phase 4（编排）
  LLMService            TimelineFormatter    CRUD 拆分          主 agent 拆分
  PersonaLoader         DebouncedPipeline    收敛裸 SQL         Worker 错误处理
                        ImageProcessor       业务逻辑移出 ORM    去重收尾
```

## 预期 Commit 计划

约 10-12 个 commit，每个 commit 闭环（写测试 → 建抽象 → 迁移调用方 → 删旧代码）。具体拆分在实现计划中细化。

## 工作方式

- TDD：先写测试（红），再写最小实现（绿），再重构
- 每个 commit 编译通过 + 测试通过
- 一个 PR 合入 main
