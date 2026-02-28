# chiwei-platform

Monorepo，所有应用在 `apps/` 下。部署在 K8s `prod` namespace。

## 项目结构

```
apps/
  paas-engine/    # PaaS 引擎 (Go) - 管理应用构建和蓝绿部署
  lite-registry/  # 泳道注册表 (Go) - Watch K8s Services，提供泳道路由数据
```

## 通用规范

- 镜像 tag: git short hash（如 `fd8ebe9`）
- 镜像仓库、代理、registry 等敏感配置通过环境变量和 K8s Secret 管理，不写入代码

## PaaS Engine

### 架构

- Go 1.25, chi 路由 + GORM ORM + PostgreSQL
- 蓝绿双泳道（blue/prod），互相部署对方
- Kaniko 构建 Job 在 `paas-builds` namespace
- 认证: `X-API-Key` header

### 核心概念

| 概念 | 说明 |
|---|---|
| **ImageRepo** | 镜像构建配置（registry 地址、git 仓库、Dockerfile 路径等），多个 App 可共享同一个 ImageRepo |
| **App** | 运行配置（关联 ImageRepo、端口、命令、环境变量等），port=0 表示 Worker（不暴露端口） |
| **Build** | 一次镜像构建（Kaniko Job），挂在 ImageRepo 下 |
| **Release** | 将某个镜像 tag 部署到某个泳道，生成 K8s Deployment + Service |
| **Lane** | 部署泳道（prod/blue/dev/feature-xxx），通过 LaneRouter SDK + K8s Service DNS 路由 |

关系：`ImageRepo（构建配置）→ Build（构建镜像）`，`App（运行配置）→ Release（部署到泳道）`，App 通过 `image_repo` 字段关联 ImageRepo。

### 开发

```bash
cd apps/paas-engine
make build    # 编译
make test     # 测试
make lint     # go vet
```

### 部署（根目录 Makefile）

通用命令通过 `APP=` 参数指定应用，适用于任意服务。

```bash
# 一键部署：构建 → 等待 → 发布到指定泳道（默认 prod）
make deploy APP=my-service [LANE=dev]

# paas-engine 蓝绿自部署（构建 → 等待 → prod → blue）
make self-deploy

# 仅发布（不构建），用于切换泳道/回滚
make release APP=<app> LANE=prod [TAG=zzz]

# 按 app+lane 删除 Release
make undeploy APP=<app> LANE=dev

# 查看状态（不传 APP 看全部）
make status [APP=xxx]

# 查看最近成功构建
make latest-build APP=<app>
```

### 关键路径

| 层 | 路径 |
|---|---|
| 入口 | `apps/paas-engine/cmd/paas-engine/main.go` |
| HTTP 路由 | `apps/paas-engine/internal/adapter/http/router.go` |
| 领域模型 | `apps/paas-engine/internal/domain/` |
| K8s 适配器 | `apps/paas-engine/internal/adapter/kubernetes/` |
| 配置 | `apps/paas-engine/internal/config/config.go` |

### 环境变量

| 变量 | 说明 | 存储位置 |
|---|---|---|
| `DATABASE_URL` | PostgreSQL 连接串 | Secret `paas-engine-secret` |
| `API_TOKEN` | API 认证 token | Secret `paas-engine-secret` |
| `DEPLOY_NAMESPACE` | 部署 namespace | App envs |
| `KANIKO_IMAGE` | Kaniko 镜像 | App envs |
| `KANIKO_CACHE_REPO` | Kaniko 远程层缓存 repo（空则禁用缓存） | App envs |
| `BUILD_HTTP_PROXY` | 构建 Pod 代理 | App envs |
| `REGISTRY_MIRRORS` | Docker Hub 镜像源 | App envs |
| `INSECURE_REGISTRIES` | 不安全 registry | App envs |

### K8s 资源

| 资源 | Namespace | 说明 |
|---|---|---|
| SA `deploy-api` | prod | paas-engine 的 ServiceAccount |
| ClusterRole `deploy-api` | - | deployments, services, jobs, secrets |
| Secret `paas-engine-secret` | prod | DATABASE_URL, API_TOKEN |
| Secret `harbor-secret` | prod, paas-builds | Harbor registry 凭证 |

### API

```
GET    /healthz                                        # 健康检查（无需认证）

# Apps
POST   /api/v1/apps/                                   # 创建应用
GET    /api/v1/apps/                                    # 列出应用
GET    /api/v1/apps/{app}/                              # 获取应用
PUT    /api/v1/apps/{app}/                              # 更新应用
DELETE /api/v1/apps/{app}/                              # 删除应用
GET    /api/v1/apps/{app}/logs                          # 运行日志

# Builds（挂在 App 下，内部通过 app.image_repo 关联）
POST   /api/v1/apps/{app}/builds/                      # 触发构建
GET    /api/v1/apps/{app}/builds/                       # 列出构建
GET    /api/v1/apps/{app}/builds/latest                 # 最近成功构建
GET    /api/v1/apps/{app}/builds/{id}/                  # 获取构建状态
POST   /api/v1/apps/{app}/builds/{id}/cancel            # 取消构建
GET    /api/v1/apps/{app}/builds/{id}/logs              # 构建日志

# Image Repos（镜像构建配置）
POST   /api/v1/image-repos/                             # 创建 ImageRepo
GET    /api/v1/image-repos/                             # 列出 ImageRepo
GET    /api/v1/image-repos/{repo}/                      # 获取 ImageRepo
PUT    /api/v1/image-repos/{repo}/                      # 更新 ImageRepo
DELETE /api/v1/image-repos/{repo}/                      # 删除 ImageRepo

# Releases
POST   /api/v1/releases/                                # 创建/更新 Release
GET    /api/v1/releases/                                # 列出 Release
DELETE /api/v1/releases/?app=xxx&lane=yyy               # 按 app+lane 删除 Release
GET    /api/v1/releases/{id}/                           # 获取 Release
PUT    /api/v1/releases/{id}/                           # 更新 Release
DELETE /api/v1/releases/{id}/                           # 删除 Release

# Lanes（泳道）
POST   /api/v1/lanes/                                   # 创建泳道
GET    /api/v1/lanes/                                   # 列出泳道
GET    /api/v1/lanes/{lane}/                            # 获取泳道
DELETE /api/v1/lanes/{lane}/                            # 删除泳道
```

### 泳道路由

泳道路由基于 K8s Service DNS，不依赖 Istio。核心组件：

- **Lite-Registry**（`apps/lite-registry/`）：Watch K8s Services，聚合 `service → {lanes, port}` 映射，API: `GET /v1/routes`
- **LaneRouter SDK**（`packages/ts-shared/`, `packages/py-shared/`）：轮询 Lite-Registry，根据 `x-lane` header 拼接 `{app}-{lane}:port`，泳道不存在时 fallback 到 `{app}:port`（prod）

详见 [docs/archive/lane-routing.md](docs/archive/lane-routing.md)。

### 注意事项

- kaniko git context 必须用 `git://` 前缀，不能用 `https://`
- git ref 支持分支名、tag（`v*` 开头）、commit hash
- paas-engine 的 Makefile（`apps/paas-engine/Makefile`）仅用于开发编译测试
