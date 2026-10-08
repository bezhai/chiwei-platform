---
paths:
  - "apps/paas-engine/**"
---

# PaaS Engine 开发指南

## 核心概念

| 概念 | 说明 |
|---|---|
| **ImageRepo** | 镜像构建配置（registry、git 仓库、Dockerfile 路径），多 App 可共享 |
| **App** | 运行配置（关联 ImageRepo、端口、命令、环境变量），port=0 = Worker |
| **Build** | 一次镜像构建（Kaniko Job），挂在 ImageRepo 下 |
| **Release** | 部署到某泳道，生成 K8s Deployment + Service |

关系：`ImageRepo → Build`，`App → Release`，App 通过 `image_repo` 关联 ImageRepo。

## 关键路径

| 层 | 路径 |
|---|---|
| 入口 | `cmd/paas-engine/main.go` |
| HTTP 路由 | `internal/adapter/http/router.go` |
| 领域模型 | `internal/domain/` |
| K8s 适配器 | `internal/adapter/kubernetes/` |
| 配置 | `internal/config/config.go` |

## 开发

```bash
cd apps/paas-engine
make build    # 编译
make test     # 测试
make lint     # go vet
```

注意：`apps/paas-engine/Makefile` 仅用于开发编译测试。

## 环境变量

paas-engine 自身也是一个 PaaS App。环境变量说明见 `docs/config-management.md` 的「PaaS Engine 自身环境变量」。

日常变更必须走 PaaS API（ConfigBundle / App envs / Release envs），不要直接改 K8s Secret/ConfigMap。`internal/config/config.go` 是代码侧读取变量的单一来源。

## K8s 资源

| 资源 | Namespace | 说明 |
|---|---|---|
| SA `deploy-api` | prod | paas-engine 的 ServiceAccount |
| Role（绑定 SA `deploy-api`） | prod | 部署用，不是 ClusterRole。deployer 写 `{app}-{lane}-config` Secret 用到 secrets 的 get / create / update |
| Role `deploy-api-builds` | paas-builds | paas-engine 在这里建构建和 CI 测试 Job、读 Pod 日志。secrets 只给 get / create / update（维护 git 凭据 Secret），没有 delete、patch |
| Secret `paas-engine-secret` | prod | 初始化凭证资源，非日常配置入口 |
| Secret `harbor-secret` | prod, paas-builds | Harbor registry 凭证，手工创建 |
| Secret `kaniko-git-auth-<lane>` | paas-builds | 每个 paas-engine 实例按自己的 `GITHUB_TOKEN` 维护一份（前缀由 `KANIKO_GIT_AUTH_SECRET_PREFIX` 配置），带 `managed-by: paas-engine`。结果态，不手改；下掉的实例留下的不会被自动删除 |

## 注意事项

- kaniko git context 必须用 `git://` 前缀，不能用 `https://`
- git ref 支持分支名、tag（`v*` 开头）、commit hash
- kaniko 克隆认证用 `GIT_USERNAME` / `GIT_PASSWORD`，不要用 `GIT_TOKEN`：v1.24.0 把它当用户名、密码为空，还会盖掉另外两个变量
- paas-engine 写 Secret 只用 get / create / update（`applySecret`），paas-builds 的 Role 没给别的动词
- 全部构建都 401 `Repository not found` 先查 token 是否失效，见 `docs/config-management.md`「构建克隆用的 GitHub token」
