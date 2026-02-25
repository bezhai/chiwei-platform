# Agent 模块路线图

> 从 `agent-module-analysis.md` 迁移，2026-02-17 更新

---

## 已完成项

| 项目 | 完成时间 | Commit |
|------|---------|--------|
| ModelBuilder TTL 缓存 + 并行 IO 优化 | 2025-02 | — |
| Langfuse 惰性初始化 + prompt 缓存 | 2025-02 | — |
| 测试体系建设（基础 54 tests） | 2025-02 | — |
| 上下文构建 asyncio.TaskGroup 并行化 | 2025-02 | — |
| 废弃工具清理（search_messages 等） | 2026-02 | 7c85830 |
| 两层重试机制（SDK max_retries + agent 级） | 2026-02 | f36bfc8 |
| L2/L3 记忆系统移除 | 2026-02 | — |
| ContextSchema → AgentContext 迁移 | 2026-02 | — |

---

## 待实施路线图

### P1: Post Graph 后处理设计

Agent 输出后需要经过两个阶段的后处理：

**Phase 1 — Blocking（阻塞，影响最终输出）**

- 输出安全检测：封禁词匹配 + LLM 内容审核
- 若检测不通过，替换为安全回复

**Phase 2 — Fire-and-forget（异步，不阻塞响应）**

- 质量评分 → 写入 Langfuse evaluation
- 记忆提取 → 从对话中自动提取关键信息
- 对话摘要 → 生成会话级别摘要

**目录结构设计：**

```
graphs/post/
├── state.py              # PostState 定义
├── graph.py              # 编排入口
├── safety_graph.py       # Phase 1: 安全检测子图
├── background_graph.py   # Phase 2: 后台任务子图
└── nodes/
    ├── safety_check.py   # 封禁词 + LLM 审核
    ├── quality_score.py  # 质量评分
    ├── memory_extract.py # 记忆提取
    └── summarize.py      # 对话摘要
```

---

### P1: Pre Graph 安全检测扩展

在现有 3 种检测（关键词、注入、政治）基础上新增节点：

| 优先级 | 检测类型 | 实现方式 | 说明 |
|--------|---------|---------|------|
| P1 | NSFW 文本检测 | LLM | 检测色情、暴力等不安全文本 |
| P1 | 频率限流 | Redis sliding window | 防止单用户高频调用 |
| P2 | PII 检测 | Regex + NER | 检测身份证、手机号、银行卡等个人信息 |
| P2 | 多模态 Jailbreak | 多模态 LLM | 检测图片中的越狱指令 |

**PreState 扩展字段：**

```python
class PreState(TypedDict):
    # 现有字段...
    user_id: str       # 用于频率限流
    chat_id: str       # 用于群级别策略
    image_urls: list[str]  # 用于多模态安全检测
```

新增节点只需在 `graphs/pre/nodes/` 新增函数 + 在 `graph.py` 中注册。

---

### P2: Multi-Agent 编排

基于 LangGraph 构建 Supervisor 模式：

```
                ┌─────────────┐
                │  Supervisor │  (路由 + 委派)
                └──────┬──────┘
          ┌────────────┼────────────┐
          ▼            ▼            ▼
    ┌──────────┐ ┌──────────┐ ┌──────────┐
    │ Chat     │ │ Research │ │ Creative │
    │ Agent    │ │ Agent    │ │ Agent    │
    └──────────┘ └──────────┘ └──────────┘
```

- **Chat Agent**：日常轻量对话
- **Research Agent**：深度搜索 + 多轮信息收集 + 总结
- **Creative Agent**：图片生成 + 创意写作

已有的 `AgentRegistry` + `AgentConfig` 天然支持注册多个 agent。

---

### P2: 测试体系扩展

在现有 54 个测试基础上补充：

```
tests/
├── core/
│   ├── test_agent.py              # ChatAgent 初始化、stream、run、重试
│   ├── test_config.py             # AgentRegistry 注册/获取
│   └── test_context.py            # AgentContext 字段测试
├── tools/
│   ├── test_search_web.py         # Web 搜索 happy/error path
│   ├── test_search_history.py     # 历史搜索逻辑
│   └── test_generate_image.py     # 图片生成
├── graphs/
│   └── test_post_graph.py         # Post Graph 端到端
└── domains/
    └── test_stream_chat.py        # 主流程集成测试
```

---

### P3: 可观测性增强

- **Token 用量追踪**：按 agent / tool / model 维度统计 prompt/completion tokens
- **工具调用指标**：调用频率、成功率、平均耗时、失败原因分布
- **复杂度分类评估**：分类结果与实际 tool 调用次数的匹配度
- **用户反馈闭环**：反馈回传到 Langfuse evaluation
- **Trace 关联**：Pre Graph / Main Agent / Post Graph 串联到同一条 trace

---

## 已搁置 / 低优先级

| 项目 | 原因 |
|------|------|
| 客户端工厂二次 DB 查询优化 | ModelBuilder TTL 缓存已缓解，收益低 |
| Agent 条件重建（避免每次 _init_agent） | prompt_vars 每次可能不同，需要更细粒度的缓存 key 设计 |
| Tool 插件化机制 | 当前工具数量少，静态注册够用 |
