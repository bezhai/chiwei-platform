# 自研 Agent Runtime 调研报告（去 langchain / langgraph）

> 状态：前期调研，未动代码。
> 目标读者：bezhai。
> 写作日期：2026-06-01。
> 一句话：把 `agent-service` 里基于 langchain `create_agent` + langgraph 的 agent 层，替换成一套自己掌控的、架在各 provider 原生 API 上的轻量 runtime。

---

## 0. 给赶时间的人：结论先放这

1. **赤尾的 agent 根本没用 langgraph 的图编排。** 没有 `StateGraph`、没有 `add_node/add_edge`、没有 conditional edges、没有 checkpointer/MemorySaver。整个 `app/agent/core.py` 对 langgraph 的依赖就一行 `from langchain.agents import create_agent`（它内部是 langgraph 实现的预制 ReAct agent），加上工具里用 `langgraph.runtime.get_runtime()` 注入上下文。所以这次**不是重写编排引擎，而是手写一个 ReAct 循环 + 一个多 provider 客户端适配层**。

2. **整个 agent 层的公共接口非常窄**：就是一个 `Agent` 类的三个方法 `run()` / `stream()` / `extract()`，被全仓 ~23 处调用。只要这三个方法签名和语义不变，23 个调用方一行都不用改。这是这次重写最大的有利条件。

3. **真正的硬骨头有四块**：(a) Gemini 原生线协议（请求/响应/streaming/tool-call/多模态格式跟 OpenAI 完全两套，而主模型恰恰是 gemini）；(b) streaming 那条路要精确复刻现有的 `finish_reason` / text→tool 边界 / 分段信号契约；(c) langfuse trace 现在是 langchain `CallbackHandler` 自动埋的，去掉后要手动埋；(d) deepseek 的 `reasoning_content` 透传（现在靠一个 langchain 子类 patch 两处）。

4. **provider 线协议第一批只要两个 adapter**：OpenAI Chat Completions（prod 里 9 个 provider 走它）+ Gemini 原生（2 个 provider，主模型）。Responses API 只有 grok 一个用，二期再说。

5. **工具 schema 生成不难。** 25 个工具的入参类型很朴素（`str` / `int` / `list[str]` / `Optional` / `Annotated[..., Field(ge,le,description)]`），没有嵌套 pydantic、没有复杂泛型。自写一个签名反射的 schema 生成器，工作量可控，且能让 25 个工具函数一行不改。

6. **架构方向已拍板**（2026-06-01 决策）：**归一化内核 + 各 provider 原生适配器**——内部定义中立的 Message / ToolDef / ToolCall / StreamChunk 类型，下面挂薄适配器把中立类型翻译成各家原生 wire。不走"全部塞 OpenAI 兼容网关"那条低公分母路线。

下面是支撑这些结论的细节，以及一份待一起过的**决策清单（第 8 节）**。

---

## 1. 现状：langchain / langgraph 到底用在哪

全仓共 **26 个文件**（不含 tests）依赖 langchain/langgraph。按"用到的能力"归类，实际只用了下面这几样：

### 1.1 消息类型（`langchain_core.messages`）— 15 个文件
`HumanMessage` / `AIMessage` / `SystemMessage` / `ToolMessage` / `AIMessageChunk` / `BaseMessage`。纯数据载体，承载 role + content + `additional_kwargs`（reasoning_content 存这里）。**替换难度：低**，几个 dataclass/pydantic 就能替，唯一要小心的是多模态 content 块的格式（`[{"type":"image_url",...}]`）各家不一样。

### 1.2 LLM 客户端（`langchain_openai` / `langchain_google_genai`）— `app/agent/models.py`
`ChatOpenAI` / `AzureChatOpenAI` / `ChatGoogleGenerativeAI`，外加一个自定义子类 `_ReasoningChatOpenAI`（`models.py:30-92`）专门给 DeepSeek 保留 `reasoning_content`——langchain 自己会在两处丢这个字段（解析响应时、拼下一轮请求时），这个子类把两处都 patch 了。**替换难度：中-高**，这是工作量最大的一块，详见第 5、6 节。

### 1.3 工具抽象（`langchain.tools.@tool`）— 12+ 个文件
所有工具用 `@tool` 装饰器，从函数签名 + `Annotated[..., Field(...)]` 自动反射出 JSON schema 喂给 LLM。**替换难度：中**，但因为参数类型朴素（见 1.5），实际可控。

### 1.4 agent 编排（`langchain.agents.create_agent` + `langgraph.runtime`）— `app/agent/core.py` + 8 个工具文件
- `create_agent(model, tools, context_schema=AgentContext)`（`core.py:145`）是唯一的"编排"调用，内部是个标准 ReAct 循环。
- `agent.ainvoke(...)` / `agent.astream(..., stream_mode="messages")` 驱动它。
- `recursion_limit=12`（`core.py:74`）控制最多转几步。
- 8 个工具用 `get_runtime(AgentContext).context` 拿上下文。
- **没有任何 StateGraph / 节点 / 边 / checkpointer**。`grep` 验证：零个 `StateGraph`、`add_node`、`add_conditional_edges`、`MemorySaver`、`interrupt`。
**替换难度：中**（循环本身是确定性、可测的几百行）。

### 1.5 结构化输出（`with_structured_output`）— `app/agent/core.py:256`
只在 `Agent.extract()` 用，绕过 ReAct 循环直接让 model 返回 pydantic 对象。主战场是 4 个安全 guard（见第 4 节）。**替换难度：中**，改成各家原生的 structured output（gemini `responseSchema` / openai `response_format: json_schema`）+ pydantic 校验。

### 1.6 streaming / trace
- streaming：`agent.astream(stream_mode="messages")` 吐 `AIMessageChunk` / `ToolMessage`，由 `app/chat/stream.py` 消费（见第 3 节）。
- trace：`from langfuse.langchain import CallbackHandler`（`core.py:39`），塞进 `config["callbacks"]`，LLM 调用 / 工具调用 / token / latency 全自动进 langfuse。**去掉 langchain 后这顿免费午餐没了，要手动埋**——这是容易被忽略的硬骨头，项目还有硬规"所有 LLM 调用必须接 trace"。

### 1.7 不依赖 langchain 的部分（保留）
- **model 解析**（`models.py:116-217`）：`model_id` → DB 查 `model_provider` / `model_mappings` → 拿 api_key/base_url/client_type，5 分钟 TTL 缓存。整块保留。
- **prompt 管理**（`prompts.py`）：langfuse SDK 取 prompt + lane 路由 + 10s 缓存。`compile_to_messages` 把 langfuse prompt 按 role 编译成消息——这里对 langchain 的依赖**只有 message 类型**，换成我们自己的类型即可。
- **memory / 对话历史存储**：自己的 DB（`common_message` 表）+ Qdrant，跟 langchain 无关。

---

## 2. 赤尾 agent 真正需要的能力（能力规格）

把上面"白嫖的东西"翻译成"新 runtime 必须提供的能力清单"，这就是自研层的验收口径：

1. **统一 Agent 入口**，三个方法签名/语义不变：
   - `run(messages, *, prompt_vars, context, max_retries) -> AIMessage`：跑完 ReAct 循环，返回最后一条 assistant 消息。
   - `stream(messages, *, prompt_vars, context, max_retries) -> AsyncGenerator[chunk]`：逐 token 流式吐，同时穿插 tool 调用。
   - `extract(response_model, messages, *, prompt_vars, max_retries) -> BaseModel`：结构化输出，不走工具循环。
   - 构造参数：`AgentConfig(prompt_id, model_id, trace_name, recursion_limit)` + `tools` + `model_kwargs`（如 `reasoning_effort`）+ `update_trace`。
2. **ReAct 循环**：喂 LLM → 解析 `tool_calls` → 按名 dispatch → 并发执行 → 结果包成 tool 结果消息接回 → 再喂 LLM；到 `recursion_limit` 或 LLM 不再要工具时停。
3. **工具系统**：工具定义（保留 `@tool` 写法或改显式 schema）+ 从签名生成 JSON schema + 注册表（`BASE_TOOLS` / `ALL_TOOLS`）+ dispatch + 错误包装（`@tool_error` → `ToolOutcomeError` dict，结构 `{kind, message, detail}`）。
4. **工具上下文注入**：替代 `get_runtime(AgentContext)`。`AgentContext` 是个 frozen dataclass（`message_id` / `chat_id` / `persona_id` / `image_registry` / `features` dict），用 `contextvars` 包一层即可。
5. **多 provider 客户端**：归一化的 `ainvoke` / `astream`，下挂 OpenAI / Gemini 原生 adapter。复用现有 DB model 解析。
6. **streaming 契约**（见第 3 节）：text token、`finish_reason`（`content_filter` / `length`）、text→tool 边界信号。
7. **结构化输出**：各家原生 structured output + pydantic 校验。
8. **trace**：手动给每次 LLM 调用 / 工具调用建 langfuse span，粒度对齐现在的 CallbackHandler。
9. **重试**：`run`/`extract` 用现成的 `app.capabilities.retry`；`stream` 因为"已 yield 不能重放前缀"，沿用现在的 inline 退避逻辑（`core.py:214-240`）。

---

## 3. streaming 契约（这条路只有一个调用方，但必须精确）

只有**主聊天**这一条路用 `stream()`（`app/chat/agent_stream.py:123`）。消费端是 `app/chat/stream.py` 的 `handle_token`，它对 token 的期待是新 runtime 必须满足的契约：

- token 是 `AIMessageChunk` 或 `ToolMessage`。
- `AIMessageChunk.response_metadata["finish_reason"]`：`"content_filter"` → 发安全错误消息；`"length"` → 追加"（后续内容被截断）"。
- `AIMessageChunk.text`：文本增量。
- `AIMessageChunk.tool_call_chunks`：非空且当前轮已有文本 → 注入 `---split---` 分段标记（赤尾把一段话拆成多条飞书消息靠这个）。
- `ToolMessage`：静默消费（计数，重置当前轮文本标记）。

也就是说，Gemini/OpenAI 两个 streaming adapter 都得把各自原生的 SSE chunk **归一化成带这些字段的中立 chunk**。这是 streaming adapter 的硬性输出格式，不能少字段。

---

## 4. 调用方全覆盖（~23 处，新 Agent 必须对所有人 drop-in）

`Agent` 被全仓这些地方实例化调用。按用法归类（这决定哪些能力第一批必须就绪）：

**stream + tools（1 处，主聊天，唯一流式路径，主模型 gemini）**
- `app/chat/agent_stream.py:123` — `Agent(cfg, tools=ALL_TOOLS).stream(...)`

**run + tools（多轮工具循环，非流式）**
- `app/memory/reviewer/light.py:54`、`heavy.py:40` — reviewer，带 `make_reviewer_tools()`（6 个记忆图工具）
- `app/life/engine.py:179`、`state_sync.py:91` — life tick，带单个动态 `commit_life_state` 工具
- `app/agent/tools/delegation.py:42` — **子 agent**：`deep_research` 工具内部 `Agent(_RESEARCH_CFG, tools=[search_web], update_trace=False).run()`，共享父 context

**run 无工具（单发 LLM，非流式）**
- `app/memory/voice.py:64` — voice 生成
- `app/nodes/memory_pipelines.py:161` — afterthought
- `app/life/glimpse.py:100` — 主动观察
- `app/life/schedule.py:106/125/150` — curator / writer / critic 三连
- `app/life/sister_theater.py:30`、`wild_agents.py:27` — 其他 life agent

**extract（结构化输出，无工具，无历史，4 处安全 guard）**
- `app/nodes/safety.py:134/155/176/256` — 注入 / 敏感政治 / NSFW / 输出安全。统一模式：`Agent(_GUARD_*, model_kwargs={"reasoning_effort":"low"}, update_trace=False).extract(PydanticModel, messages=[], prompt_vars={...})`，返回带 `confidence` 字段的 pydantic，按阈值判定。

**门面**
- `app/capabilities/agent.py` — `AgentRunner`，给 dataflow @node 预置一个 Agent，`run/stream/extract` 直接透传。drop-in 后零改动。

**观察**：`run()` 是绝对主力（十几处），`stream()` 只有主聊天一处，`extract()` 集中在安全。意味着可以**先把 `run` + `extract` + OpenAI adapter 跑通**（覆盖 guard / 各 life / memory agent），最后再啃 `stream` + Gemini adapter（主聊天）。天然的分期边界。

---

## 5. provider 现状（来自 prod `model_provider` 表的真实数据）

只读查询结果（已剔除密钥）：

| client_type | 数量 | provider | 归属线协议 |
|---|---|---|---|
| `openai` | 6 | 302.ai / 302.oversea / devbox / Moonshot / ollama / openrouter | OpenAI Chat Completions |
| `deepseek` | 1 | DeepSeek（带 reasoning_content patch） | OpenAI Chat Completions 变体 |
| `ark` | 1 | doubao（豆包，无专门分支 → 落默认） | OpenAI Chat Completions |
| `azure-http` | 1 | azure | OpenAI Chat Completions（Azure 鉴权 + api-version + deployment 当 model） |
| `google` | 2 | 302-gemini / gemini（gemini 走代理） | **Gemini 原生** |
| `openai-responses` | 1 | grok | OpenAI Responses API |

**推论**：
- 第一批做 **OpenAI Chat Completions adapter**（一把覆盖 openai 6 + deepseek + ark/doubao + azure 变体 = 9 个 provider）和 **Gemini 原生 adapter**（2 个，主模型），就能覆盖生产绝大多数流量。
- Azure 不是独立 wire，是 chat completions 的鉴权/URL 变体，同一 adapter 加个 flag 即可。
- `openai-responses` 只有 grok 一个；grok 本身支持 OpenAI 兼容 chat completions，**大概率能并进 chat completions adapter，省掉一个 Responses adapter**（待决策 D3）。
- DeepSeek 的 `reasoning_content` 是真在用，得在 chat completions adapter 里原生处理（不再靠 langchain 子类 patch）。

---

## 6. 目标架构设计（归一化内核 + 原生适配器）

```
                 业务层（chat / life / memory / safety / nodes）
                              │  只认 Agent.run / stream / extract
                              ▼
        ┌─────────────────────────────────────────────┐
        │  Agent（编排层）                              │
        │   - ReAct 循环（run / stream）                │
        │   - extract（结构化输出，不走循环）           │
        │   - 工具 dispatch + @tool_error 包装          │
        │   - contextvars 注入工具上下文                │
        │   - langfuse span 埋点                        │
        └─────────────────────────────────────────────┘
                              │  中立类型 Message / ToolDef / ToolCall / StreamChunk
                              ▼
        ┌─────────────────────────────────────────────┐
        │  ModelClient（归一化客户端接口）              │
        │   - ainvoke(messages, tools, **kw) -> Message │
        │   - astream(...) -> AsyncIterator[StreamChunk]│
        │   - structured(messages, schema) -> dict      │
        └─────────────────────────────────────────────┘
              │                         │
              ▼                         ▼
   ┌────────────────────┐   ┌────────────────────┐
   │ OpenAIChatAdapter  │   │ GeminiAdapter      │     （Responses adapter 二期/可能并掉）
   │  含 azure 变体      │   │  原生 generate/    │
   │  含 deepseek        │   │  streamGenerate    │
   │  reasoning 处理     │   │  content + thinking│
   └────────────────────┘   └────────────────────┘
              │                         │
              └──── 复用现有 DB model 解析（model_provider/model_mappings + TTL 缓存）
```

**模块落点（建议，待 spec 细化）**：
- `app/agent/types.py`：中立 Message / ToolCall / ToolDef / StreamChunk（替代 `langchain_core.messages`）。
- `app/agent/client/base.py`：`ModelClient` 抽象接口。
- `app/agent/client/openai_chat.py`：OpenAI Chat Completions adapter（含 azure / deepseek 变体）。
- `app/agent/client/gemini.py`：Gemini 原生 adapter。
- `app/agent/loop.py`：ReAct 循环（run/stream 共用）。
- `app/agent/toolspec.py`：从函数签名生成 JSON schema + 注册表。
- `app/agent/runtime_ctx.py`：contextvars 上下文注入（替代 `get_runtime`）。
- `app/agent/trace.py`：langfuse 手动埋点 helper。
- `app/agent/core.py`：`Agent` 类，对外签名保持不变。
- `app/agent/models.py`：保留 DB 解析，但返回值从"langchain BaseChatModel"改为"我们的 ModelClient"。

**底层 HTTP**：直接用各家原生 SDK（`openai` SDK 走 chat completions；`google-genai` SDK 或直接 HTTP 走 gemini），还是统一自己用 `httpx` 打——这是决策 D7。

---

## 7. 硬骨头与风险清单

1. **Gemini 原生 wire（最大）**：请求体（`contents` + `parts` + `tools`/`functionDeclarations`）、响应（`candidates`）、streaming（`streamGenerateContent` 的 chunk 格式）、tool-call（`functionCall` part）、多模态（`inlineData` / `fileData`）、thinking——全和 OpenAI 两套。主模型是它，错不起。
2. **streaming 契约复刻**：第 3 节那套 `finish_reason` / `tool_call_chunks` 边界 / 分段，两个 adapter 都要正确归一化，否则飞书分段、内容过滤、截断提示会坏。
3. **trace 从自动变手动**：CallbackHandler 现在自动记 LLM input/output、tool call、token、latency、trace 嵌套。手动埋要保证嵌套关系（一个 Agent run 一个 trace，里面每次 LLM 调用/工具调用是子 span），且子 agent（deep_research）的 trace 要挂在父下面。漏埋违反项目硬规。
4. **deepseek reasoning_content**：解析响应时捞出来存到中立 Message 的 reasoning 字段，拼下一轮请求时塞回 `reasoning_content`，并把 content 归一化成纯字符串（DeepSeek 拒绝 null/数组 content）。现在的 `_ReasoningChatOpenAI` 是行为参考。
5. **多模态 content 块**：工具返回的 `list[dict]`（`read_images` / `search_images` / `generate_image` 返回 `image_url` 块）要能被两个 adapter 正确翻译成各自的图片输入格式。
6. **结构化输出跨 provider**：`extract` 要在 OpenAI（`response_format: json_schema`）和 Gemini（`responseSchema`）上都能稳定吐 pydantic，且失败时的报错/重试行为一致。
7. **重试与"已 yield 不可重放"**：stream 路径的重试语义要保留（首 token 吐出后不再重试，否则重复前缀）。

**不是风险的（先排除焦虑）**：图编排（根本没用）、checkpointer（自己 DB）、对话历史（自己 DB）、prompt 管理（langfuse，不动）、工具 schema 复杂度（很低）。

---

## 8. 决策清单（明天一起过；带我的推荐）

> D1 已在今天对话里拍板，其余 D2–D9 待定。每条给了推荐，明天逐条确认/推翻。

**D1（已定）｜客户端骨架** → 归一化内核 + 原生适配器。✅

**D2｜迁移方式** —— 推荐：**原地重写 `Agent` 内部，对外 `run/stream/extract` 签名语义完全不变**，23 个调用方零改动。按 refactoring-rules 不留任何兼容层/shim，旧 langchain 实现直接删。理由：接口窄、调用方多，保接口是风险最小的切法。

**D3｜第一批做几个 adapter** —— 推荐：**OpenAI Chat Completions（含 azure/deepseek 变体）+ Gemini 原生**两个。grok 的 `openai-responses` 先验证能否并进 chat completions，能就不单独做 Responses adapter。

**D4｜结构化输出实现** —— 推荐：**各 provider 原生 structured output**（OpenAI `response_format: json_schema`、Gemini `responseSchema`）+ pydantic 校验；不用"工具调用伪装结构化"那种 hack。对齐项目"优先 function calling/JSON 而非抠自然语言"的口径。

**D5｜工具定义方式** —— 推荐：**保留 `@tool` 写法，自写签名反射的 schema 生成器**，25 个工具函数一行不改。理由：参数类型已确认朴素（`str`/`int`/`list[str]`/`Optional`/`Annotated[Field]`），反射器覆盖这几种 typing 构造即可，比逐个改成显式 pydantic 模型省事且零行为风险。

**D6｜message 类型** —— 推荐：**自己的中立类型，一刀切**，不保留 `langchain_core.messages` 当过渡（refactoring-rules 禁兼容层）。`compile_to_messages` 同步改成产出新类型。

**D7｜底层 HTTP** —— 待定：用各家**原生 SDK**（`openai` + `google-genai`，省心但多两个依赖、且要驯服 SDK 的封装）vs 统一**自己 `httpx`** 打（完全掌控 wire/proxy/重试，但要自己写两套请求/解析）。倾向**原生 SDK**起步（尤其 gemini 的多模态/streaming 自己写易错），但 OpenAI 侧因为要 deepseek reasoning patch，可能 httpx 更干净。**这条想听你意见。**

**D8｜trace 粒度** —— 推荐：**对齐现状**——每个 `Agent.run/stream/extract` 一个 trace，内部每次 LLM 调用、每次工具调用各一个子 span，子 agent 挂父 trace 下。用 langfuse SDK 的 span/generation API 手动埋。

**D9｜验证策略** —— 推荐：按项目铁律，**不靠 code review / 静态审计**，部 `coe-agentrt` 独立泳道、绑 dev bot，用真实对话 + 各 provider（gemini 主聊 / deepseek / guard）跑端到端；安全 guard 的 `extract` 单独造样例验证结构化输出稳定性。coe 需先把 schema + 种子数据复刻到 chiwei-test。

**另需你确认的范围问题**：
- `app/memory/reviewer/tools.py`、`app/life/*` 里那些 `@tool` 是否也一起切（它们也走 `Agent.run` + 工具）？推荐**一起切**，否则新旧两套工具机制并存更乱。
- 是否同时把 `app/agent/models.py` 里 embedding / image-gen 那几个非 chat 的 model 构建也归一化？推荐**本次只动 chat model**，embedding/image-gen 不在 agent 循环里，单独评估，避免范围蔓延。

---

## 9. 工作量与分期（粗估，待 spec 细化）

| 阶段 | 内容 | 验收 |
|---|---|---|
| P1 | 中立类型 + ModelClient 接口 + OpenAI Chat adapter（含 deepseek reasoning）+ ReAct 循环（run）+ extract + contextvars + 工具 schema 生成 + trace 埋点 | guard 4 检查、各 life/memory agent（非流式）在 coe 跑通 |
| P2 | streaming 路径 + Gemini 原生 adapter（含多模态、thinking、streaming 归一化） | 主聊天在 coe 跑通，飞书分段/过滤/截断正常 |
| P3 | 删干净所有 langchain/langgraph import + 依赖；grok/Responses 收尾决策；回归 | `grep langchain/langgraph` 零残留，pyproject 移除四个依赖 |
| P4 | coe 端到端 + 上 prod cutover | 真实流量验证通过 |

粗略量级：核心两到三周，cutover 验证另算。**P1 跑通就能证明大循环和 OpenAI 侧没问题，P2 的 Gemini + streaming 是真正的风险集中区**，建议 P2 多留 buffer。

---

## 10. 下一步

1. 明天和 bezhai 过 **D2–D9 决策清单**，定稿范围。
2. 据此写正式 **spec**（强制覆盖调用方全覆盖 + 数据&部署影响 + 粗颗粒 task），触发 codex T1 review。
3. spec 定稿后按 P1→P4 实现，全程 coe 泳道验证，禁静态审计找 bug。

---

### 附：本报告依据的关键代码位置
- `app/agent/core.py`：`Agent` 类 / `create_agent` / run/stream/extract / trace。
- `app/agent/models.py`：DB model 解析 + provider dispatch + `_ReasoningChatOpenAI`。
- `app/agent/context.py`：`AgentContext` 字段。
- `app/agent/tools/__init__.py`：`BASE_TOOLS` / `ALL_TOOLS`。
- `app/agent/tools/_common.py` + `outcome.py`：`@tool_error` / `ToolOutcomeError`。
- `app/agent/prompts.py`：langfuse prompt + `compile_to_messages`。
- `app/chat/stream.py`：streaming token 契约。
- `app/chat/agent_stream.py`：主聊天调用点。
- `app/nodes/safety.py`：4 个 extract guard。
- `app/capabilities/agent.py`：`AgentRunner` 门面。
- prod `model_provider` 表：provider / client_type 真实分布（第 5 节）。
