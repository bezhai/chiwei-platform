# chiwei-platform

Monorepo，所有应用在 `apps/` 下。部署在 K8s `prod` namespace。

## 项目结构

```
apps/
  paas-engine/    # PaaS 引擎 (Go) - 管理应用构建和蓝绿部署
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
# 普通服务一键部署（构建 → 等待 → release 到 prod）
make deploy APP=my-service

# paas-engine 蓝绿自部署（构建 → 等待 → prod → blue）
make self-deploy

# 分步操作
make build APP=<app>                          # 触发构建
make build-status APP=<app> BUILD_ID=<id>     # 查看构建状态
make build-wait APP=<app> BUILD_ID=<id>       # 轮询等待构建完成
make status APP=<app>                         # 查看各泳道状态
make release APP=<app> LANE=prod              # 发布到指定泳道
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
| `BUILD_HTTP_PROXY` | 构建 Pod 代理 | App envs |
| `REGISTRY_MIRRORS` | Docker Hub 镜像源 | App envs |
| `INSECURE_REGISTRIES` | 不安全 registry | App envs |

### K8s 资源

| 资源 | Namespace | 说明 |
|---|---|---|
| SA `deploy-api` | prod | paas-engine 的 ServiceAccount |
| ClusterRole `deploy-api` | - | deployments, services, jobs, secrets, virtualservices |
| Secret `paas-engine-secret` | prod | DATABASE_URL, API_TOKEN |
| Secret `harbor-secret` | prod, paas-builds | Harbor registry 凭证 |

### API

```
GET    /healthz                                # 健康检查（无需认证）
POST   /api/v1/apps/                           # 创建应用
PUT    /api/v1/apps/{app}/                      # 更新应用
POST   /api/v1/apps/{app}/builds/               # 触发构建
POST   /api/v1/apps/{app}/builds/{id}/cancel    # 取消构建
GET    /api/v1/apps/{app}/builds/{id}/logs      # 构建日志
POST   /api/v1/releases/                        # 创建/更新 Release
DELETE /api/v1/releases/{id}/                    # 删除 Release
POST   /api/v1/lanes/                           # 创建泳道
```

### 注意事项

- kaniko git context 必须用 `git://` 前缀，不能用 `https://`
- git ref 支持分支名、tag（`v*` 开头）、commit hash
- paas-engine 的 Makefile（`apps/paas-engine/Makefile`）仅用于开发编译测试
