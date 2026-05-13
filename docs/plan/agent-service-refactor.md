# agent-service Framework 重构 Plan

2026-05-12 起，针对三条主线：应用层更易用、基建层更易维修、上面起一层"赤尾的角色 framework"。本文件是可执行任务清单，不展开代码细节。背景诊断材料见 memory `project_dataflow_phase7.md` / `project_backlog.md` 及本会话 Explore 输出。本版本已消化 codex T2 review（详见末尾附录）。

## 目标

- 业务作者最终只回答三件事：产生/消费什么 Data？哪个 Node？需要什么 capability？
- framework 内部错误处理分级清楚，不再出现 broad-except 吞 malformed row 这类反复发生的同族 bug。
- agent/memory/life/chat 按"刺激→内心独白→决策→留痕→沉淀→复盘"循环重写，禁止工程脑替赤尾决策（阈值/计数器/随机池/9 层 if-append 拼字符串等）。
- 每项 framework 改造落地前必须在 coe 泳道按真机演练剧本跑通才合 PR。

## Phase A：基础治理（前置一切）

- **A0. 统一节点执行契约**（B 阶段共同前提）：单点定义 `@node` 函数签名（输入/返回值）、自动 emit 语义、异常如何上浮、`on_error` 声明的语义（**边级唯一**）、wire 链上各装饰器（durable/transient/debounce/fan_out_per）的组合规则、错误分类边界（runtime / wire / node / tool / capability 各自负责哪一档，含 5 个 capability 异常类）。**产物是 `docs/guides/dataflow-node-contract.md` + 补齐缺失断言**。缺失断言清单：W2a（with_latest join key compile-time）、W4a（cross-app transport startup）、W14（on_error 非 dlq 必 durable startup）、cron/interval emit 异常 vs infra 异常分清（A2 阶段补）、`swallow_and_log` policy（B4 阶段补）。没有这个 contract，B1/B4/B7/B8 会互相覆盖语义。
- **A1. framework 启动统一化**：main.py HTTP 入口和 workers/runtime_entry worker 入口走同一套启动抽象，节点注册阶段和 capability 可用阶段分清楚。验收：business 模块顶层 `from app.runtime.db import emit_tx` 永远不再形成循环依赖，proactive.py 函数局部 import workaround 删除。
- **A2. framework 内部错误处理分级**（落地 A0 错误分类的 framework 内部部分）：dispatcher / MQSource / 所有 framework 内部 `except` 必须按 A0 定义分为"无害（兜底+重试）"和"致命（抬出来报警）"。验收：grep `except Exception` 在 runtime/ 内只剩明确分级的位置，每处都有理由注释。
- **A3. 功能层异常上浮契约**（落地 A0 错误分类的 capability/infra 部分）：所有 functional layer 调用（qdrant/llm/embed/redis/http capability）不准 catch 异常返回 bool/None，必须按 A0 定义向上抛指定类型。验收：grep "return False" / "return None" 在 capabilities/ + infra/ 内只剩有明确语义的位置。

A0 必须最先做。A1/A2/A3 可在 A0 完成后并行（不必互相串行）。

## Phase B：framework 能力补全（依赖 A0/A2/A3）

- **B1. emit_and_wait**（依赖 A0）：业务发完消息后能原地等异步节点回包。替代 `chat/pre_safety_gate.py` 全局字典 + 手写 Future。验收：演练剧本"100 turn 对话 + 中途 kill pod + 重启后正确处理 in-flight"通过。
- **B2. fan_out_wait**：同步进程内并发跑一组独立任务并收集结果（带超时/异常解包）。替代 `chat/_context_images.py` / `life/schedule.py` / `nodes/safety.py` 手拼的 `asyncio.gather + wait_for`。验收：演练剧本"safety 三个 LLM 检查并发跑、其中一个慢响应、剩两个超时不被拖累"通过。
- **B3. @retry 装饰器**：网络调用本地重试（指数退避+错误类型白名单）。替代 `agent/core.py` 30 行手工 retry。跟现有 `.retry()`（跨进程消息重投）是两件事，两个都要。验收：演练剧本"LLM 调用模拟 429/timeout 三次后成功"通过。
- **B4. 补 swallow_and_log wire policy**（依赖 A0 + A3）：on_error 已是边级（`wire.py` + `durable.py` 已实现 dlq / ignore-duplicate / manual-review 三种），本项把第 4 种 `swallow_and_log` 补上——节点抛异常显式吃掉 + log + ack，禁止默认开启。同时移除所有业务节点的 `except Exception as e: logger.error(); raise` 兜底，节点直接抛、wire on_error 决定路径。**禁止节点级 on_error**（见 A0 contract §1）。验收：演练剧本"某节点强制 raise，按 wire 声明路由到 DLQ / review / swallow 三种路径"通过；所有 nodes/ 里 try-except 兜底清零。
- **B5. Redis capability**：公开接口（含 Lua 脚本执行/原子 INCR/批量 pipeline），自动处理 lane 前缀和 metrics。替代 `infra/image.py` 直接调 `redis.eval()`。验收：演练剧本"两个泳道并发跑去重 Lua，互不影响"通过。
- **B6. DLQ admin capability**：包 `OutboxEmitter` / `WireSpec` / `aio_pika.Message` / `delete_inflight` / `RabbitMQManagementClient`，给业务节点用的公开 API。验收：dlq_admin.py 不再 import runtime/ 任何内部模块，演练剧本"DLQ 重投 5 条消息全部成功 ack"通过。
- **B7. 声明式 per-persona fan-out**（依赖 A0）：wire 链上写 `wire(MinuteTick).fan_out_per(persona).to(check_drift)`，替代 life_dataflow.py 手写 `_fan_out_per_persona`。验收：7 处 cron tick 全部改成声明式，演练剧本"某 persona 失败不影响其他 persona"通过。
- **B8. @node 自动 emit 文档化 + 补单测覆盖**（依赖 A0）：自动 emit 已在 commit 0ea263b 实现（`node.py:92-99` wrapper 检查 isinstance Data 自动 emit），本项不再"从头实现"，只做两件事：(1) 单元测试系统化覆盖"返回 Data 自动流入下游 / 返回 None 跳过 / 手动 emit + return 同 Data 重复 emit 的禁用场景"；(2) 把 N6 边界写进 contract（已完成）。验收：单测齐全 + 演练剧本"chat → safety → recall 链路 emit 自动衔接"通过。

## Phase C：业务收口（依赖对应 B 项）

- **C1. chat_node:128 改用 emit_and_wait**（依赖 B1）：删 `asyncio.create_task(run_pre_safety_via_graph(...))`，改成 emit PreSafetyRequest + emit_and_wait 等 verdict。验收：B1 演练剧本同时通过。
- **C2. dlq_admin.py 改用 B6 capability**（依赖 B6）：清干净所有 runtime/ 内部 import。验收：grep 0 匹配。
- **C3. agent/tools/_common.py @tool_error 改用 framework 契约**（依赖 A3 + B4）：移除 catch 异常字符串化，让 LLM 看到 typed outcome；工具节点配 `@node(on_error=...)` 声明。验收：演练剧本"工具调用失败 LLM 收到结构化错误而非字符串"通过。
- **C4. life_dataflow 改用声明式 fan-out**（依赖 B7）：删 `_fan_out_per_persona` helper，wire 上改写。验收：演练剧本"MinuteTick 触发 N 个 persona 全部 fan out 成功 + 单 persona 失败隔离"通过。
- **C5. infra/image 改用 B5 capability**（依赖 B5）：替换直接 `redis.eval` 调用。验收：演练剧本"两个泳道并发去重互不污染"通过。

## Phase D：角色 framework 草图（主线 2，依赖 B1/B2/B4/B7）

- **D1. 角色循环抽象设计**：定义"刺激/内心独白/决策/effect 留痕/沉淀/复盘"六个 primitive 的接口、状态边界、observability hook（langfuse trace 强制）。**同步定义三件事**：(a) **赤尾决策归属边界**——哪些决策必须由 LLM 输出（不准代码替她拍板）、哪些是赤尾给出的 policy 输入由代码执行、哪些是 framework 不可见的运维参数；(b) **policy 输入形态**——配置 vs prompt vs Data，怎么版本化、怎么 hot reload、谁有改它的权限；(c) **dataflow substrate 接口边界**——角色层调底层用什么 API，底层暴露给角色层哪些保证（消息可靠投递、跨服务 trace、lane 隔离），底层禁止暴露什么（aio_pika 对象/outbox 内部状态/DLQ replay 直接 API）。设计完调 codex T1 review。
- **D2. chat 流程按角色循环重写**：chat_node + agent_stream 改成"刺激→内心独白→决策→effect"序列。删 prompt_vars 手工拼接，inner_context 不再用 9 层 if-append 字符串。**灰度策略**：保留现有 chat 流程，新链路加 lane 隔离，coe 泳道全量验证 + prod 单 persona 灰度后切换。验收：演练剧本"赤尾正常对话 + langfuse trace 每阶段可见 + 关键决策有 typed event"通过。
- **D3. memory 按角色循环重写**：memory/sections 改成赤尾的"我记得什么"结构化输出（不是字符串拼接）；reviewer/ drift+afterthought 改成角色复盘 primitive 实例；MAX_PER_SUBJECT=5 / _LOOKBACK_HOURS=2 这种硬编码搬到 D1 定义的 policy 输入。灰度同 D2。
- **D4. life 按角色循环重写**：life/engine 改成角色刺激源；proactive `random.random() >= 0.15` / `HOURLY_PROACTIVE_LIMIT` 搬到 policy 输入，或让赤尾自己决定（提示词层而不是分支层）。灰度同 D2。

## Phase E：弃用 langgraph（主线 3，挂 backlog L2）

D 落定后再启动。自己写 agent loop 让 LLM 直接看到 stimulus / inner monologue / tool call / observation / decision，而不是被 langgraph 状态机包装。此 phase 现在不动。

## 测试体系（贯穿 A-D 每个 Phase）

- **F0. coe 泳道演练平台搭建**（跟 A 并行启动，分两层产出）：
  - **环境前置**：ConfigBundle `coe` 覆盖（按 backlog 已定的 `class_overrides[coe]` + `required_keys[coe]` 机制）、PG/Redis/MQ/Qdrant schema 自动从 prod 拉脚本初始化到 chiwei-test、dev bot 所需 user/persona/bot 种子数据从 prod dump 同步、agent-service + vectorize-worker 同步部署到同 lane（一镜像多服务）。
  - **演练剧本本体**：chat 100 turn 内存检查、同 chat_id 并发 5 条消息打 pre_safety_gate、kill pod 看 in-flight、RabbitMQ 重启看 emit_delayed、24 小时长跑看 langgraph 累积状态、依赖中断（PG/Redis/MQ down）、配置漂移（运行中改 ConfigBundle）。剧本本身是产出物，每个 Phase 任务的验收都从这里挑剧本对应。
- **F1. 单测过一遍**（依赖 A0 contract）：单测保留标准——能在 A0 contract 文档 + 26 条已修底座 bug 清单里指认出"删了这条测试会让哪条 contract / 哪类历史 bug 漏 catch"。没法指认就删。每次 framework 改造前先过对应区域的单测。**不允许"全删"或"全留"——必须每条单测都给具体守护对象**。
- **F2. ship 门禁**（跟 backlog L2 "Dev Workflow v2 Phase 4" 合并）：任何 framework 改造类 release 未经 coe 演练通过的不准合到 prod。但门禁必须配紧急绕过：
  - **适用范围**：只对 framework / runtime / dataflow / capabilities 类改动强制；hotfix（不动 framework 文件、只改业务行为）不卡。
  - **override 机制**：单次 release 可由 bezhai 走 `/ops` 审批 override，审计记录写入 paas_engine.audit_log，override 原因必填。
  - **降级**：coe 泳道环境本身坏掉（chiwei-test 容器集不可用）时门禁自动降级为 warning + 必填理由放行，不强制 block。

## 执行顺序总结

A0 最先做（contract 落地）→ A1/A2/A3 并行 → B1-B8 并行（B1/B4/B7/B8 依赖 A0，B4/C3 依赖 A3）→ C 各项等对应 B 完成 → D1 等 B1/B2/B4/B7 完成 → D2/D3/D4 等 D1 完成且灰度策略就绪 → E 等 D 完成。F0 跟 A0 并行启动，F1 等 A0 contract 出来再过单测，F2 ship 门禁等 F0 演练平台就绪再上。

## 已知未列入此 Plan 的尾巴

- backlog L0/L1 的小坑（ruff.toml deprecated、Phase 7 minor 尾巴、CI grep gate ghost FAILURE 等）不在此 plan 范围，基础治理完后视情况处理。
- ORM → Data 迁移（memory `project_orm_migration_to_data.md`）是独立线，跟此 plan 不交叉。

## 附录：codex T2 review 已采纳清单

- **必改 1**：B 内部"完全并行"不成立 → **采纳**，新增 A0 统一节点执行契约作为 B1/B4/B7/B8 共同前提。
- **必改 2**：A2/A3 没说错误分类边界归属 → **采纳**，A0 contract 显式定义 runtime / wire / node / tool / capability 各自的错误分类职责，A2/A3 落地为 A0 的 framework 内部 + capability 部分。
- **必改 3**：F2 ship 门禁缺紧急绕过 → **采纳**，F2 增加适用范围、override 审批、降级策略三项细则。
- **建议 1**：F0 缺 coe 环境前置 → **采纳**，F0 拆为"环境前置"+"剧本本体"两层产出。
- **建议 2**："演练剧本通过"作唯一验收会漏 contract 回归 → **采纳**，F1 单测保留标准绑定 A0 contract 文档 + 26 条已修底座 bug 清单作为 baseline。
- **建议 3**：D 草图不足以指导主线 2 → **采纳**，D1 增加"决策归属边界 / policy 输入形态 / dataflow substrate 接口边界"三项必出产物，D2/D3/D4 增加灰度共存策略。
