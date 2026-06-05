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
| ClusterRole `deploy-api` | - | deployments, services, jobs, secrets |
| Secret `paas-engine-secret` | prod | 初始化凭证资源，非日常配置入口 |
| Secret `harbor-secret` | prod, paas-builds | Harbor registry 凭证 |

## 注意事项

- kaniko git context 必须用 `git://` 前缀，不能用 `https://`
- git ref 支持分支名、tag（`v*` 开头）、commit hash
