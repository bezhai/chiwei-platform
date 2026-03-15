# CI Pipeline 体系规划

PaaS Engine 内置的自动化 CI 流水线，目标：推送代码到特性分支 → 自动触发 → 单元测试 → 构建 → 部署 → E2E 验证。

## 架构概览

```
Git Push → GitPoller (60s轮询 GitHub API)
               ↓
         PipelineService (异步编排)
               ↓
    ┌──────────┼──────────┐──────────┐
    ↓          ↓          ↓          ↓
 unit-test   build      deploy      e2e
 (K8s Job)  (Kaniko)  (Release)  (未实装)
```

### 关键设计

- **幂等性**: 同一 commit SHA 只触发一次 pipeline（`ExistsByCommitSHA` 检查）
- **状态机**: `pending → running → succeeded | failed | cancelled`
- **日志三级降级**: Pod logs → Loki → DB 存储
- **Callback 异步同步**: K8s Informer 监听 Job 状态变化，更新 DB

## 实现阶段

### Phase 0: 核心三阶段 ✅

三阶段串行、阶段内 Job 并行：

1. **unit-test** — K8s Job（git clone init container + runtime test container）
2. **build** — 复用 Kaniko 构建
3. **deploy** — 复用 Release 服务

测试命令硬编码在 `resolveUnitTestCommand()`:

| 服务 | runtime | 命令 |
|------|---------|------|
| paas-engine | go | `go test ./... -v -count=1` |
| agent-service | python | `uv run pytest tests/ -v` |
| lark-server | bun | `bun test` |
| lark-proxy | bun | `bun test` |
| tool-service | python | `uv run pytest tests/ -v` |

API 端点:

| 端点 | 方法 | Makefile |
|------|------|----------|
| `/api/paas/ci/register` | POST | `make ci-init` |
| `/api/paas/ci/` | GET | `make ci-list` |
| `/api/paas/ci/{lane}/trigger` | POST | `make ci-trigger` |
| `/api/paas/ci/{lane}/runs` | GET | `make ci-status` |
| `/api/paas/ci/{lane}/` | DELETE | `make ci-cleanup` |
| `/api/paas/ci/runs/{id}/` | GET | `make ci-logs` |
| `/api/paas/ci/runs/{id}/cancel` | POST | — |
| `/api/paas/ci/runs/{id}/logs` | GET | — |

### Phase 0.5: Git Poller 自动触发 ✅

- 轮询 GitHub API 检测注册分支的新 commit
- 配置: `GITHUB_TOKEN` + `CI_GIT_REPO`（环境变量）
- 跳过 main/master 分支
- 间隔可配: `GIT_POLL_INTERVAL`（默认 60s）

### Phase 1: pipeline.yml 声明式配置

**目标**: 从 monorepo 根目录读取 `pipeline.yml`，替代硬编码。

结构体已定义（`domain/pipeline_config.go`）:

```yaml
# pipeline.yml（预期格式）
services:
  paas-engine:
    runtime: go
    unit_test: "go test ./... -v -count=1"
  agent-service:
    runtime: python
    unit_test: "uv run pytest tests/ -v"
    e2e_test: "uv run pytest tests/e2e/ -v"
    e2e_env:
      PAAS_API: "http://paas-engine:8080"

lark_flow:
  runtime: bun
  cmd: "bun run test:e2e:lark"
  timeout: "5m"
```

实现要点:
- 从 git clone 后的 workspace 中读取 `pipeline.yml`
- 校验格式 + 合并默认值
- 替换 `resolveUnitTestCommand()` 中的 switch-case

### Phase 2: E2E 测试

**目标**: 部署后自动验证服务可用性。

已预留:
- `StageE2E` stage 类型
- `JobType`: `"e2e-http"`（HTTP 接口测试）、`"e2e-lark"`（飞书全链路）
- `ServiceTestConfig.E2ETest` + `E2EEnv`
- `LarkFlowConfig`（runtime/cmd/timeout/env）

预期流程:
```
... → deploy → e2e-http（各服务接口验证） → e2e-lark（飞书消息收发）
```

实现要点:
- e2e-http: 部署完成后，在同 namespace 启动 K8s Job 访问服务端点
- e2e-lark: 需要 dev bot 凭证，发送测试消息并验证回复
- 超时和重试策略

### Phase 3: GitHub Webhook（可选）

**目标**: 替代轮询，推送即触发，更实时且不占 API 配额。

考虑点:
- 需要公网可达的 webhook 端点（或通过反向代理暴露）
- 签名验证（`X-Hub-Signature-256`）
- 与 GitPoller 可共存，webhook 优先、poller 兜底

## 环境变量

| 变量 | 说明 | 存储 |
|------|------|------|
| `CI_NAMESPACE` | CI Job 运行的 namespace | app envs（默认 `paas-builds`）|
| `CI_GIT_REPO` | monorepo 地址（user/repo 格式）| app envs |
| `GITHUB_TOKEN` | GitHub PAT | app envs（建议迁至 Secret）|
| `GIT_POLL_INTERVAL` | 轮询间隔 | app envs（默认 60s）|

## 数据库表

| 表 | 说明 |
|---|------|
| `ci_configs` | 泳道 CI 注册（lane UNIQUE）|
| `pipeline_runs` | pipeline 执行记录（commit_sha 索引）|
| `stage_runs` | 阶段记录（seq 保证顺序）|
| `job_runs` | 作业记录（含日志文本）|

## 代码结构

```
apps/paas-engine/internal/
  domain/
    pipeline.go          # PipelineRun/StageRun/JobRun/CIConfig 模型
    pipeline_config.go   # pipeline.yml 解析结构（Phase 1 预留）
  port/
    pipeline.go          # TestExecutor/CIConfigRepository/PipelineRunRepository 接口
  service/
    pipeline_service.go  # 核心编排（654 行）
    git_poller.go        # GitHub API 轮询
  adapter/
    kubernetes/
      test_executor.go   # K8s Job 创建/监听/日志
    http/
      pipeline_handler.go
    repository/
      pipeline_repo.go   # GORM 持久化
```
