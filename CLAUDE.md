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
  lark-proxy/     # 飞书 webhook 入口 (Bun/TS) - 查 lane_routing 决定路由
  lark-server/    # 飞书消息处理 (Bun/TS) - 同一镜像产出 3 个独立 Deployment（见下方映射表）
  agent-service/  # AI 对话引擎 (Python) - 同一镜像产出 3 个独立 Deployment（见下方映射表）
  api-gateway/    # 反向代理入口 (Go)
```

### 镜像与服务映射（一镜像多服务）

**一个 Docker 镜像可以产出多个独立的 K8s Deployment。** 它们是不同进程、不同 Pod，日志和排查必须按实际服务名来，不能混淆。

| 镜像（ImageRepo） | 产出的 K8s Deployment | 角色 |
|---|---|---|
| lark-server | **lark-server** | HTTP 服务，处理飞书消息 |
| lark-server | **recall-worker** | 消费 RabbitMQ recall 队列 |
| lark-server | **chat-response-worker** | 消费 RabbitMQ 回复队列，发飞书消息 |
| agent-service | **agent-service** | HTTP 服务，AI 对话 |
| agent-service | **arq-worker** | 异步任务 worker |
| agent-service | **vectorize-worker** | 向量化 worker |

**常见错误：查 chat-response-worker 的日志时用 `make logs APP=lark-server`，这是错的。** chat-response-worker 是独立 Deployment，必须用 `make logs APP=chat-response-worker`。同理 recall-worker、arq-worker、vectorize-worker 都是独立服务。

## 核心数据流

### 飞书消息处理

```
飞书 → lark-proxy:3003 (webhook 入口, 查 lane_routing 决定路由)
     → lark-server:3000 (消息处理, 注入 x-lane 到 context)
     → agent-service:8000 (AI 对话, 工具调用)
     → RabbitMQ: safety_check → vectorize → recall 队列
     → chat-response-worker → lark-server → 飞书回复
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
SDK (agent-service/lark-server) → paas-engine (读取 API, /internal/dynamic-config/resolved)
```

- 基础设施连接（DB/Redis）走 ConfigBundle（部署时环境变量）
- 业务行为参数（模型/阈值/flag）走 Dynamic Config（运行时 SDK 读取，10s 缓存）
- 接入指南和 API 详见 `docs/dynamic-config.md`

## 通用规范

- 镜像 tag: 语义化版本号（如 `1.0.0.2`），由 PaaS Engine 服务端分配
- **配置管理统一走 ConfigBundle API**（`/api/paas/config-bundles/`），禁止直接操作 K8s Secret/ConfigMap。查看 app 最终配置用 `GET /api/paas/apps/{app}/resolved-config?lane=prod`。

## 开发流程

**禁止直接在 main 分支上修改代码。** 每次需求变更：

1. **需求分析**：用 `superpowers:brainstorming` 探索意图、澄清需求、对比方案
2. **出方案**：用 `superpowers:writing-plans` 生成分步实现计划（超 10 行改动必须）
3. **切分支**：从 main 切分支（可用 `/worktree` skill）
4. **执行方案**：用 `superpowers:executing-plans` 按计划逐步实现，写代码遵循 `superpowers:test-driven-development` 红-绿-重构循环
5. **遇到 bug**：用 `superpowers:systematic-debugging` 结构化排查（与"3 次后必须停"互补）
6. `git push` 到远端（Kaniko 从 git remote 拉代码，本地 commit 不够）
7. 部署独立泳道（如 `feat-alert-v2`），不直接用 `dev`
8. 飞书测试必须绑定 dev bot: `/ops bind bot dev <lane>`
9. **完成前验证**：用 `superpowers:verification-before-completion` 确保有证据再宣称完成
10. 验收后解绑 + 下泳道: `/ops unbind bot dev` → `make undeploy APP=<app> LANE=<lane>`
11. `ghc pr merge --squash` 合并到 main（**必须用项目 `ship` skill，禁止用 `superpowers:finishing-a-development-branch`**）
12. `make self-deploy`（paas-engine）或 `make deploy APP=<app>`

### 上线前必须完成的检查（TODO）

代码改完、泳道验证通过后，**合码前**逐条过：

- [ ] **调用方全覆盖**：`grep` 被修改函数的所有调用方，列出每个调用场景（群聊/私聊/rebuild/afterthought/...），确认每个场景下的行为是否正确。不是看一眼，是每个场景都要有运行验证的证据。
- [ ] **数据读写一致**：如果改了写入的目标表，确认所有读取方也已切换。如果新建了表，确认旧表的读取方不会读到空数据。
- [ ] **副作用清单**：列出这次改动的所有副作用（新表、新 prompt、新 agent 注册、DB schema 变更），确认每个都已就绪。
- [ ] **部署影响**：如果有后台异步任务正在运行（rebuild、afterthought），部署会杀掉它们。部署前确认没有正在跑的任务，或者明确告知用户"部署会中断 X"。

### superpowers 禁用项

以下 superpowers skill 与项目自有 skill 冲突，**禁止使用**：

- `superpowers:finishing-a-development-branch` → 用项目 `/ship` 替代（遵守合码铁律）
- `superpowers:using-git-worktrees` → 用项目 `/worktree` 替代（遵守部署约束）

## 部署命令

部署命令必须显式写 `GIT_REF`，如 `make deploy APP=lark-proxy GIT_REF=main`，禁止省略。

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
4. **一镜像多服务同步。** 部署 agent-service 后必须同步 release arq-worker 和 vectorize-worker；部署 lark-server 后必须同步 recall-worker 和 chat-response-worker。

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
