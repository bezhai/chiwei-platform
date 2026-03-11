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

| 变量 | 说明 | 存储位置 |
|---|---|---|
| `DATABASE_URL` | PostgreSQL 连接串 | Secret `paas-engine-secret` |
| `API_TOKEN` | API 认证 token | Secret `paas-engine-secret` |
| `DEPLOY_NAMESPACE` | 部署 namespace | App envs |
| `KANIKO_IMAGE` | Kaniko 镜像 | App envs |
| `KANIKO_CACHE_REPO` | Kaniko 远程层缓存 repo（空则禁用） | App envs |
| `BUILD_HTTP_PROXY` | 构建 Pod 代理 | App envs |
| `REGISTRY_MIRRORS` | Docker Hub 镜像源 | App envs |
| `INSECURE_REGISTRIES` | 不安全 registry | App envs |

## K8s 资源

| 资源 | Namespace | 说明 |
|---|---|---|
| SA `deploy-api` | prod | paas-engine 的 ServiceAccount |
| ClusterRole `deploy-api` | - | deployments, services, jobs, secrets |
| Secret `paas-engine-secret` | prod | DATABASE_URL, API_TOKEN |
| Secret `harbor-secret` | prod, paas-builds | Harbor registry 凭证 |

## 注意事项

- kaniko git context 必须用 `git://` 前缀，不能用 `https://`
- git ref 支持分支名、tag（`v*` 开头）、commit hash
