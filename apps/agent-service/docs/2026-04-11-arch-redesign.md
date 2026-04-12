# agent-service 架构重设计

## 背景

agent-service 是赤尾的认知引擎。当前代码 15,414 行（非 test），分散在 13 个目录、6 层 agents 子模块中，存在职责混乱、重复代码、抽象割裂等问题。本次重构从领域出发重新设计模块边界。

## 核心认知

1. **agent-service 的核心是 agent。** 所有"思考"操作统一走 agent 抽象，有工具/没工具是参数差异，不是抽象差异。
2. **禁止裸调 LLM。** 除 embedding 和画图外，所有 LLM 调用都经过 agent。
3. **重构是重新设计抽象，不是搬代码。** 按目标架构重写，旧代码仅作行为参考。

## 目标架构

```
app/
  agent/        # 核心：统一思考入口
  chat/         # 对话管线
  memory/       # 记忆与身份
  life/         # 自主生活
  data/         # 数据访问
  infra/        # 外部系统客户端 + 配置
  workers/      # 执行基底（薄层）
  skills/       # 技能系统
  api/          # HTTP 路由
```

---

## 模块详细设计

### `agent/` — 统一思考入口

赤尾所有"思考"都经过这里。这是 agent-service 存在的理由。

**内部结构：**

```
agent/
  core.py         # 统一 runner（有工具→多步推理，没工具→单次调用）
  models.py       # 模型管理（provider、model mapping、LangChain model 构建）
  prompts.py      # Prompt 管理（Langfuse prompt 拉取、变量注入）
  tracing.py      # Langfuse trace/observation 管理
  embedding.py    # Embedding 调用（例外，不走 agent runner）
  tools/          # agent 可用的所有工具
    search.py     # 网页搜索 + 图片搜索 + 重排
    history.py    # 聊天历史查询 + 群成员查询
    image.py      # 图片读取 + 图片生成
    recall.py     # 记忆召回（PG 全文检索 + 向量检索）
    delegation.py # 子 agent 委派（research 等）
    sandbox.py    # 沙箱代码执行
    skill.py      # 技能加载
```

**核心接口：**

```python
class Agent:
    """统一的思考入口。有工具就多步推理，没工具就单次调用。"""

    async def run(
        self,
        prompt_id: str,
        variables: dict,
        *,
        tools: list[Tool] | None = None,
        messages: list[BaseMessage] | None = None,
        response_model: type[T] | None = None,  # 传了就返回结构化结果
    ) -> T | str: ...

    async def stream(
        self,
        prompt_id: str,
        variables: dict,
        *,
        tools: list[Tool] | None = None,
        messages: list[BaseMessage] | None = None,
    ) -> AsyncGenerator[str, None]: ...
```

**设计要点：**
- `response_model` 参数：传 Pydantic model 就自动解析返回值，消灭所有 `_extract_text()` 和手动 JSON 解析
- 重试逻辑只在 core.py 定义一次
- Langfuse trace 自动接入，调用方不需要关心
- tools 定义在 agent/ 下，调用方（chat/memory/life）组合使用哪些工具

---

### `chat/` — 对话管线

一条消息从进入到回复的完整路径。

**内部结构：**

```
chat/
  pipeline.py      # stream_chat 主编排
  context.py       # 上下文构建（历史、图片、persona、记忆注入、日程注入）
  stream.py        # 流式处理（token 缓冲、safety race、截断检测）
  safety.py        # Pre-check（banned word + LLM 分类）+ Post-check（输出审查）
  post_actions.py  # 后处理触发（safety check、drift、afterthought）
  router.py        # 消息路由（@mention 路由决策）
```

**核心接口：**

```python
async def stream_chat(
    message_id: str,
    *,
    persona_id: str | None = None,
) -> AsyncGenerator[str, None]: ...
```

**设计要点：**
- safety 是 chat 的子关注点，不独立成模块。pre 在管线内执行，post 由后处理触发
- 上下文构建整合：历史消息、图片处理、persona、记忆、日程统一在 context.py
- 工具集组合在 pipeline.py 中定义，传给 agent

---

### `memory/` — 记忆与身份

赤尾如何记住、反思、演化。

**内部结构：**

```
memory/
  afterthought.py    # 事后回想：debounce → 生成经历碎片
  drift.py           # 身份漂移：debounce → 评估偏离元设定
  voice.py           # 语音风格：内心独白 + 说话风格生成
  relationships.py   # 关系记忆：从对话中提取对人的认知更新
  dreams.py          # 梦：日/周记忆压缩
  context.py         # 上下文注入：为 chat 构建要注入的记忆段落
  debounce.py        # Debounced pipeline 基类（afterthought 和 drift 共用）
```

**核心接口：**

```python
# Debounced 触发（chat 后处理调用）
afterthought.on_event(chat_id: str, persona_id: str) -> None
drift.on_event(chat_id: str, persona_id: str) -> None

# 定时生成（worker 调用）
async def generate_voice(persona_id: str) -> None
async def compress_dreams(persona_id: str, grain: Grain) -> None
async def extract_relationships(persona_id: str, chat_id: str, messages: list) -> None

# 上下文注入（chat context 调用）
async def build_memory_context(persona_id: str) -> str
```

**设计要点：**
- 所有 LLM 调用走 `agent.run(response_model=...)` 拿结构化结果
- afterthought 和 drift 共享 debounce 基类
- timeline 格式化逻辑内聚在本模块，不散布到各处

---

### `life/` — 自主生活

赤尾不在聊天时的独立存在。

**内部结构：**

```
life/
  engine.py      # Life Engine：每分钟 tick，决定活动和心情
  schedule.py    # 日程规划：月→周→日三层计划生成
  glimpse.py     # 窥屏：browsing 状态下观察群消息
  proactive.py   # 主动搭话：基于观察发起对话
```

**核心接口：**

```python
async def tick(persona_id: str) -> LifeState
async def run_glimpse(persona_id: str) -> GlimpseResult  # Enum，不是字符串
async def generate_schedule(persona_id: str, tier: ScheduleTier) -> None
async def submit_proactive(persona_id: str, ...) -> None
```

**设计要点：**
- schedule 的 Agent 管线（Ideation→Writer→Critic）在这里实现，不在 worker 里
- GlimpseResult 用 Enum
- Life Engine 状态用 dataclass，解析失败返回旧状态而非丢字段

---

### `data/` — 数据访问

**内部结构：**

```
data/
  models.py       # 所有 SQLAlchemy 模型
  session.py      # Session 管理，支持事务
  queries/        # 按领域分组的查询函数（2-3 个文件，不按表分）
```

**设计要点：**
- 不要 CRUD 目录，不要 6 个文件按表分
- 查询函数按领域分组（chat 相关、memory 相关、life 相关）
- 业务逻辑不在这层（优先级匹配、fallback 策略上移到调用方）
- 提供 session context manager 支持事务

---

### `infra/` — 外部系统

**内部结构：**

```
infra/
  config.py       # Settings
  redis.py        # Redis 客户端
  rabbitmq.py     # RabbitMQ（泳道隔离、DLX）
  qdrant.py       # Qdrant 向量数据库
  image.py        # 图片服务（飞书下载 / TOS 上传）
  lane.py         # LaneRouter 实例
```

**设计要点：**
- 全部用模块级实例，不写 `get_instance()` 手动单例
- Qdrant 从 services/ 移到这里，它是基础设施不是业务

---

### `workers/` — 执行基底

**原则：workers 只编排，不做业务。**

**内部结构：**

```
workers/
  cron.py            # 所有 cron 任务定义（调用 life/、memory/）
  chat_consumer.py   # Chat request MQ 消费者（调用 chat/）
  post_consumer.py   # Post safety MQ 消费者（调用 chat/safety）
  vectorize.py       # 向量化 MQ 消费者（调用 infra/qdrant + agent/embedding）
  common.py          # 共享工具：persona 批处理、错误处理装饰器
```

**核心共享工具：**

```python
async def for_each_persona(
    fn: Callable[[str], Awaitable[None]],
    label: str,
) -> None:
    """遍历所有 persona 执行函数，统一错误处理。"""
```

**设计要点：**
- 所有 cron worker 的 persona 迭代 + 错误处理统一为 `for_each_persona()`
- 业务逻辑在对应领域模块，worker 只是触发器

---

### `skills/` — 技能系统

保持现有设计，独立领域。

### `api/` — HTTP 路由

保持精简。middleware 合并到此模块。

---

## 被消灭的现有目录/文件

| 现有 | 去向 |
|------|------|
| `agents/core/` | → `agent/core.py` |
| `agents/clients/` | → `agent/models.py` |
| `agents/infra/` | → `agent/` (models, prompts, tracing, embedding) |
| `agents/domains/` | → `chat/` |
| `agents/graphs/` | → `chat/safety.py` |
| `agents/tools/` | → `agent/tools/` |
| `services/qdrant.py` | → `infra/qdrant.py` |
| `services/quick_search.py` | → `agent/tools/` 或 `data/` |
| `services/message_router.py` | → `chat/router.py` |
| `services/banned_word.py` | → `chat/safety.py` |
| `services/debounced_pipeline.py` | → `memory/debounce.py` |
| `services/bot_context.py` | → `chat/context.py` |
| `services/content_parser.py` | → `chat/` |
| `services/persona_loader.py` | → `data/` |
| `services/schedule_context.py` | → `life/` |
| `services/download_permission.py` | → `chat/context.py` 内联 |
| `services/timeline_formatter.py` | → `memory/` 内部 |
| `services/afterthought.py` | → `memory/afterthought.py` |
| `services/identity_drift.py` | → `memory/drift.py` |
| `services/voice_generator.py` | → `memory/voice.py` |
| `services/relationship_memory.py` | → `memory/relationships.py` |
| `services/memory_context.py` | → `memory/context.py` |
| `services/life_engine.py` | → `life/engine.py` |
| `services/glimpse.py` | → `life/glimpse.py` |
| `utils/singleton/` | 删除 |
| `utils/decorators/serializer.py` | 删除 |
| `utils/decorators/error_handler.py` | 删除 |
| `utils/content_parser.py` | 删除（转发文件） |
| `types/` | 分散到各领域模块 |
| `middleware/` | → `api/` |
| `config/` | → `infra/config.py` |
| `long_tasks/` | 待确认使用场景，可能精简或删除 |

## 验收指标

1. **非 test 代码量 -30%**（15,414 → ≤10,800）
2. **基于目标架构的新测试全部通过**
3. **grep 零残留** — 旧模块名、旧 import 路径搜不到
4. **无重复定义** — 同一逻辑只在一处
5. **无死代码** — 定义了没被 import 的函数/类/文件不存在
6. **模块数量与本文档一致** — 9 个顶层模块，不多不少
7. **ruff 零报错** — lint + format 全部通过
8. **全部代码 Pythonic** — 禁止非 Pythonic 写法
9. **对现有逻辑保持质疑** — 发现问题讨论，禁止把 bug 当 feature 搬进新架构
