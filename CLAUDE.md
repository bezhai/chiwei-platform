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
```

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

## 通用规范

- 镜像 tag: git short hash（如 `fd8ebe9`）
- 敏感配置通过环境变量和 K8s Secret 管理，不写入代码

## 开发流程

**禁止直接在 main 分支上修改代码。** 每次需求变更：

1. 从 main 切分支（可用 `/worktree` skill）
2. `git push` 到远端（Kaniko 从 git remote 拉代码，本地 commit 不够）
3. 部署独立泳道（如 `feat-alert-v2`），不直接用 `dev`
4. 飞书测试必须绑定 dev bot: `make lane-bind TYPE=bot KEY=dev LANE=<lane>`
5. 验收后解绑 + 下泳道: `make lane-unbind TYPE=bot KEY=dev` → `make undeploy APP=<app> LANE=<lane>`
6. `ghc pr merge --squash` 合并到 main
7. `make self-deploy`（paas-engine）或 `make deploy APP=<app>`

## 部署命令

```bash
make deploy APP=<app> [LANE=dev]          # 构建 → 等待 → 发布
make self-deploy                           # paas-engine 蓝绿自部署
make release APP=<app> LANE=prod [TAG=x]   # 仅发布（不构建）
make undeploy APP=<app> LANE=dev           # 删除 Release
make status [APP=xxx]                      # 查看状态
make latest-build APP=<app>                # 最近成功构建
```

## AI 行为约束

### 生产环境操作

- **写操作（PUT/POST/DELETE）影响线上前，必须先告知用户并等确认。** GET 随便做。
- **不熟悉的 API，先确认语义。** PUT 是 partial 还是 full replace？先问。
- **遇到不理解的现象，问用户而不是猜测然后改线上。**
- **出事故时聚焦用户关心的点，不要撒网式检查。**
- **e2e 测试禁止直接改线上真实资源。**

### 基础设施

- **用户说怎么做就怎么做，不要自作主张换方案。**
- **$PAAS_API 前面有反向代理，支持 x-lane 路由。** 测试用 `$PAAS_API` + `x-lane` header，不需要 port-forward。
- **不要在没有充分验证的情况下否定用户的方案。**

## 环境配置

- **GitHub CLI**: 必须用 `ghc` 而不是 `gh`（`/usr/local/bin/gh` 是公司内部工具）
- **PaaS API PUT /apps/{app}/**: merge 语义，无需带完整字段
