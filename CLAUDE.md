# chiwei-platform

## 宪法级文档（禁止修改）

**`MANIFESTO.md`（赤尾宣言）是本项目的宪法。未经 bezhai 明确许可，任何人和任何 AI 不得修改此文件。**

---

Monorepo，所有应用在 `apps/` 下。部署在 K8s `prod` namespace。

## 项目结构

```
apps/
  paas-engine/    # PaaS 引擎 (Go) - 管理应用构建和蓝绿部署
  lite-registry/  # 泳道注册表 (Go) - Watch K8s Services，提供泳道路由数据
  channel-proxy/  # 飞书 webhook 入口 (Bun/TS) - 查 lane_routing 决定路由
  channel-server/ # 飞书消息处理 (Bun/TS) - 同一镜像产出 3 个独立 Deployment（见下方映射表）
  agent-service/  # AI 对话引擎 (Python) - 同一镜像产出 3 个独立 Deployment（见下方映射表）
  api-gateway/    # 反向代理入口 (Go)
```

### 镜像与服务映射（一镜像多服务）

**一个 Docker 镜像可以产出多个独立的 K8s Deployment。** 它们是不同进程、不同 Pod，日志和排查必须按实际服务名来，不能混淆。

| 镜像（ImageRepo） | 产出的 K8s Deployment | 角色 |
|---|---|---|
| channel-server | **channel-server** | HTTP 服务，处理飞书消息 |
| channel-server | **recall-worker** | 消费 RabbitMQ recall 队列 |
| channel-server | **chat-response-worker** | 消费 RabbitMQ 回复队列，发飞书消息 |
| agent-service | **agent-service** | HTTP 服务，AI 对话 |
| agent-service | **vectorize-worker** | 向量化 worker |

**常见错误：查 chat-response-worker 的日志时用 `make logs APP=channel-server`，这是错的。** chat-response-worker 是独立 Deployment，必须用 `make logs APP=chat-response-worker`。同理 recall-worker、vectorize-worker 都是独立服务。

## 核心数据流

### 飞书消息处理

```
飞书 → channel-proxy:3003 (webhook 入口, 查 lane_routing 决定路由)
     → channel-server:3000 (消息处理, 注入 x-lane 到 context)
     → agent-service:8000 (AI 对话, 工具调用)
     → RabbitMQ: safety_check → vectorize → recall 队列
     → chat-response-worker → channel-server → 飞书回复
```

未部署泳道的服务自动 fallback 到 prod（基于 K8s Service DNS，不依赖 Istio）。

### 部署链路

```
PaaS Engine API
  → 构建: Kaniko Job (paas-builds ns) → Harbor Registry
  → 发布: K8s Deployment + Service (prod ns)
```

蓝绿部署仅限 paas-engine 自身：prod 和 blue 泳道互相部署对方（`make self-deploy`）。其他服务直接部署到 prod。

### 泳道路由

```
请求 → 反向代理 ($PAAS_API, 支持 x-lane header)
     → lite-registry (Watch K8s Services, 聚合 service → {lanes, port})
     → LaneRouter SDK (拼接 {app}-{lane}:port, 不存在则 fallback {app}:port)
```

SDK 在 `packages/ts-shared/`（TS）和 `packages/py-shared/`（Python）。

### 动态配置

```
Dashboard → monitor-dashboard → paas-engine (管理 API, /api/paas/dynamic-config/)
                                            ↕ dynamic_configs 表
SDK (agent-service/channel-server) → paas-engine (读取 API, /internal/dynamic-config/resolved)
```

- 基础设施连接（DB/Redis）走 ConfigBundle（部署时环境变量）
- 业务行为参数（模型/阈值/flag）走 Dynamic Config（运行时 SDK 读取，10s 缓存）
- 接入指南和 API 详见 `docs/dynamic-config.md`

## 通用规范

- 镜像 tag: 语义化版本号（如 `1.0.0.2`），由 PaaS Engine 服务端分配
- **配置管理统一走 ConfigBundle API**（`/api/paas/config-bundles/`），禁止直接操作 K8s Secret/ConfigMap。查看 app 最终配置用 `GET /api/paas/apps/{app}/resolved-config?lane=prod`。

## 泳道命名规范（强制，paas-engine 校验）

paas-engine `domain.ClassifyLane` fail-closed 拒绝未知前缀。**所有新泳道必须用以下命名**：

| 命名 | 基础设施 | 用途 |
|---|---|---|
| `prod` | 线上 | 生产，所有服务共用 |
| `blue` | 共用线上 | **仅 paas-engine 蓝绿自部署专用**，其他服务禁用 |
| `ppe-<name>` | 共用 prod 全部组件（PG/Redis/MQ/Qdrant/Mongo） | **功能性验证**：业务逻辑、prompt、agent 行为，对线上数据有读写 |
| `coe-<name>` | 独立离线（chiwei-test 容器集，连接串由 ConfigBundle `class_overrides[coe]` 注入） | **基建开发 / 破坏性改动**：schema 变更、消息协议变更、重写 worker，不污染 prod 数据 |

**选型口诀**：能复用线上数据且本次改动不会污染线上就 `ppe-*`，要建表 / 改协议 / 可能炸 / 改完会写脏数据的就 `coe-*`。飞书 dev bot 测试两者都可，coe 需先把所需 schema + 种子数据（user / persona / bot 配置等 dev bot 跑通必读项）从 prod 复刻到 chiwei-test，详见 `.claude/rules/e2e-testing.md`。

ConfigBundle 通过 `class_overrides[coe]` + `required_keys[coe]` 自动把 coe-* 的连接串切到 chiwei-test 容器，业务代码不感知。详见 [[project_dev_workflow_v2]]。

## 开发流程

**禁止直接在 main 分支上修改代码。** 分支由用户切好递给 Claude（worktree 不归 Claude 管），Claude 接到需求后按下面主线推进：

0. **重活优先委派子 agent（判断性建议）**：主会话可以直接读写本仓库文件，但**重活应优先委派子 agent**——大范围探索、大量 grep / 读多文件、并行实现这类会大量消耗上下文的活，派子 agent 做、主会话只接结论与 diff + 证据，目的是保护主会话的上下文。轻活（看一两个文件、小改动）主会话自己动手即可。按规模和上下文成本判断，不是机械强制。
1. **判断简单 / 复杂**：typo / rename / 一两行无行为变化的改动，主会话可直接做，跳过下面的 spec / review 流程。其他走完整流程。
2. **先 Explore，再写 spec**：建议先派 Explore 子 agent 查清调用方、现有实现、相关数据流，主对话只接结论；**禁止**凭印象写 spec。涉及大范围调研时尤其应该走 Explore，避免把试错烧在主会话上下文里。
3. **写 spec（`/spec`）**：含目标、不做什么、关键设计决策、调用方全覆盖、数据&部署影响、粗颗粒 task 清单。**spec 里的 task 只写"目标 + 产出 + 验收口径"，禁止出现代码片段 / 文件行号 / 实现步骤**；具体验证命令在实现阶段基于实际改动补齐 —— 实现细节是动手时才能生成的知识，spec 阶段预写就是想象，必失真。
4. **codex T1 review**：spec 定稿叫一次 codex，重点检查任务颗粒度是否合适、有没有藏着的实现想象。逐条采纳 / 驳回写理由，更新 spec。
5. **实现（重活建议委派子 agent，主会话也可自己动手）**：
   - **要并行就先 map 再 parallel**：并行派修改类子 agent 之前，先派一个 Explore 子 agent 摸出"哪些 task 互不碰文件"的真实分区图——按各 task 方法实际能触达的文件算，**不是**按声明产出算。据此分区结果再并行。这条在用并行子 agent 时仍然有效。
   - **无文件冲突时鼓励并行**：分区图确认互不碰文件的 task，派 `general-purpose` 子 agent 并行做，每个 agent 自己生成实现细节并产出验证证据，主会话只接产出 + 证据。这是处理大批量 / 可并行改动的推荐方式。
   - **有文件冲突 / 有依赖的 task**：按依赖顺序串行做；规模大的串行委派子 agent，规模小的主会话自己改也行。
   - 跨需求并行（多个独立 feature）走另一个 worktree / 会话。
   - **TDD 红-绿-重构**：先写测试再写实现。委派子 agent 时由该子 agent 自己完成红-绿-重构；主会话自己动手时同样先写测试。
6. **遇到死循环必停**：同一报错 ≥2 次 / 同一测试 ≥3 次 / A↔B 往返。结构化分析根因，必要时叫 codex T4 独立诊断（必须先告诉用户、等同意）。
7. **commit 前**：含设计或逻辑变动的批叫 codex T3 review。完成前必须列出验证证据（命令 + 实际输出），禁止"看着对、应该没问题"。
8. `git push` 到远端（Kaniko 从 git remote 拉代码，本地 commit 不够）。
9. 部署独立泳道（命名遵守上方规范：功能性验证用 `ppe-<name>`，基建 / 破坏性改动用 `coe-<name>`），不直接用 `dev`。
10. 飞书测试必须绑定 dev bot：`/ops bind bot dev <lane>`。
11. 验收后解绑 + 下泳道：`/ops unbind bot dev` → `make undeploy APP=<app> LANE=<lane>`。
12. `/ship` 合码并部署 prod（合码铁律见 `.claude/rules/merge-and-ship.md`）。

### 子 agent 与 codex 的使用边界

主会话可以直接读写仓库文件。子 agent 是**处理重活和并行的推荐工具**，不是机械强制——按规模和上下文成本判断：大范围探索、大量读写、可并行的批量改动派子 agent，小范围排查和小改动主会话自己做即可。

- **Explore 子 agent**：研究代码，不写代码。大范围查调用方 / 现有实现 / 类似模式、需要读多个文件回答问题时，建议派 Explore，主对话只接结论，避免把调研过程烧在主会话上下文里。小范围只看一两个文件，主会话自己 Read / Grep 即可。
- **general-purpose 子 agent（并行）**：处理大批量 / 可并行的仓库文件修改的推荐工具。**要并行就先 map 再 parallel**：并行前先派一个 Explore 子 agent 按"各 task 方法实际能触达哪些文件"摸出真实分区图（不是按声明产出算），无文件冲突的 task **鼓励并行**派多个子 agent；有冲突 / 有依赖的串行。每个 agent 拿一条 task 自己想细节、自己走 TDD 红-绿-重构（先写测试再写实现）、自己跑验证、自己报产出。规模小的改动主会话也可以自己动手。
- **应用 reviewer 反馈**：采纳 codex 反馈去改 spec 或代码，改动大就委派子 agent，改动小主会话自己改即可。
- **跨需求并行**：用 worktree + 多会话，不在一个会话里塞多个独立 feature。
- **codex**：外部 reviewer，不是 worker。T1（spec 写完）/ T2（plan 写完，本项目 plan 合并进 spec 不单独触发）/ T3（一批含设计变动的代码 commit 前）/ T4（debug 死循环，需用户先同意），详见 `~/.claude/rules/codex-collaboration.md`。

### 上线前必须完成的检查（TODO）

代码改完、泳道验证通过后，**合码前**逐条过：

- [ ] **调用方全覆盖**：`grep` 被修改函数的所有调用方，列出每个调用场景（群聊/私聊/rebuild/afterthought/...），确认每个场景下的行为是否正确。不是看一眼，是每个场景都要有运行验证的证据。
- [ ] **数据读写一致**：如果改了写入的目标表，确认所有读取方也已切换。如果新建了表，确认旧表的读取方不会读到空数据。
- [ ] **副作用清单**：列出这次改动的所有副作用（新表、新 prompt、新 agent 注册、DB schema 变更），确认每个都已就绪。
- [ ] **部署影响**：如果有后台异步任务正在运行（rebuild、afterthought），部署会杀掉它们。部署前确认没有正在跑的任务，或者明确告知用户"部署会中断 X"。

## 部署命令

部署命令必须显式写 `GIT_REF`，如 `make deploy APP=channel-proxy GIT_REF=main`，禁止省略。

```bash
make deploy APP=<app> [LANE=dev] [BUMP=minor] [VERSION=2.0.0.1] [GIT_REF=main]  # 构建 → 等待 → 发布
make self-deploy [BUMP=minor]                                      # paas-engine 蓝绿自部署
make release APP=<app> LANE=prod VERSION=1.0.0.5                   # 仅发布（不构建，用于回滚）
make undeploy APP=<app> LANE=dev                                   # 删除 Release
make status [APP=xxx]                                              # 查看状态
make latest-build APP=<app>                                        # 最近成功构建
```

### 部署铁律

1. **禁止未经泳道验证直接部署到 prod。** 任何代码改动，无论多小（"就改了一行"不是理由），必须先部署到泳道、用真实流量或 rebuild 验证通过，再走 `/ship` 上线。唯一例外：用户明确说"直接上"。
2. **部署 = 杀 Pod = 中断所有异步任务。** 部署前必须确认没有正在跑的后台任务（rebuild、afterthought 等）。如果有，要么等它跑完，要么告知用户会中断。
3. **rebuild 等批量操作的参数（persona、chat_id、时间范围）必须由用户指定。** 不要自己填默认值，不要"顺便"扩大范围。
4. **一镜像多服务同步。** 部署 agent-service 后必须同步 release vectorize-worker；部署 channel-server 后必须同步 recall-worker 和 chat-response-worker。

## AI 行为约束

### 赤尾设计原则

不要用工程思维解决 agent 的不确定性问题。
当赤尾的行为不符合预期时，正确的方向是优化她的输入（context、prompt、stimulus、agent 协作），
而不是在逻辑层加确定性规则（阈值、计数器、格式化函数、随机池、if/else 分支）。
不确定性是 agent 像人的来源，不是需要被消除的 bug。

### 生产环境操作

- **写操作（PUT/POST/DELETE）影响线上前，必须先告知用户并等确认。** GET 随便做。
- **不熟悉的 API，先确认语义。** PUT 是 partial 还是 full replace？先问。
- **遇到不理解的现象，问用户而不是猜测然后改线上。**
- **出事故时聚焦用户关心的点，不要撒网式检查。**
- **e2e 测试禁止直接改线上真实资源。**

### 基础设施

- **开发机到集群的唯一出口是 `$PAAS_API`（反向代理）。** 不要尝试直连容器（port-forward、svc.cluster.local、Pod IP、localhost:端口、psql/redis-cli），没有网络通路，hook 也会拦截。运维查询走 Dashboard API（`/ops` skill），构建/部署/日志走 `make`。
- **用户说怎么做就怎么做，不要自作主张换方案。**
- **不要在没有充分验证的情况下否定用户的方案。**
- **同一操作失败两次，必须停下来分析根因或问用户，禁止暴力重试。**

### 运维查询命令

运维查询优先走 Dashboard API（自动审计），构建/部署/日志仍走 `make`：

| 操作 | 命令 | 说明 |
|------|------|------|
| 服务状态 | `/ops status` | Dashboard API |
| Pod 状态 | `/ops pods APP [LANE]` | Dashboard API |
| 最近构建 | `/ops latest-build APP` | Dashboard API |
| 数据库查询 | `/ops-db @数据库 SQL` | `@chiwei`（业务）或 `@paas_engine`（PaaS），必须指定 |
| 泳道绑定 | `/ops bindings` / `/ops bind` / `/ops unbind` | Dashboard API |
| 审计日志 | `/ops audit` | Dashboard API |
| 应用日志 | `make logs [APP=<app>] [KEYWORD=error]` | Loki（无 Dashboard 端点） |

**排查问题时必须用 `make logs`，禁止进容器捞日志或直接调 Loki API。** 支持 APP/KEYWORD/EXCLUDE/REGEXP/SINCE 等参数，详见 Makefile。

## 环境配置

- **GitHub CLI**: 因特殊原因，必须用 `ghc` 而不是 `gh`
- **PaaS API PUT /apps/{app}/**: merge 语义，无需带完整字段
