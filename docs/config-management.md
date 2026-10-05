# 配置管理

本文说明项目配置管理规则，包括部署时环境变量、ConfigBundle、App/Release env、Dynamic Config、K8s Secret/ConfigMap 的使用边界。

## 总原则

- 不直接修改 K8s Secret / ConfigMap 来变更应用配置。PaaS 发布会重建结果态资源，手改会丢失。
- 基础设施连接和密钥走部署时配置：ConfigBundle、App envs 或 Release envs。
- 业务行为参数走 Dynamic Config：模型、阈值、feature flag 等需要运行时生效的配置。
- 真实密钥不写入 git、文档、日志、协作记录或 issue；只通过 PaaS API 写入管理面。
- 查看最终生效配置时注意接口会返回真实值，不要把生产密钥复制到协作记录或 issue。

## 配置类型

| 类型 | 用途 | 是否需要重新部署 | 入口 |
|---|---|---:|---|
| ConfigBundle | DB/Redis/MQ/Qdrant/外部 API key 等可被多个 App 复用的环境变量组 | 需要 | `/api/paas/config-bundles/` |
| App envs | 某个 App 的稳定运行参数，随所有 lane 生效 | 需要 | `PUT /api/paas/apps/{app}/` |
| Release envs | 某个 App + lane 的临时或实例级覆盖 | 需要 | `POST /api/paas/releases/` 或 `PUT /api/paas/releases/{id}/` |
| Dynamic Config | 业务行为参数：模型、阈值、开关 | 不需要，SDK 10s 缓存 | `/api/paas/dynamic-config/` |
| envFrom 引用 | 既有外部 Secret/ConfigMap 引用 | 需要 | App 的 `env_from_secrets` / `env_from_config_maps` |

## ConfigBundle

ConfigBundle 是部署时环境变量的主入口。每个 key 都是最终注入容器的环境变量名，例如 `POSTGRES_HOST`、`RABBITMQ_URL`。

字段语义：

| 字段 | 说明 |
|---|---|
| `keys` | 基线值。通常对应 prod 或所有 lane 共用的默认值 |
| `class_overrides` | 按 lane class 覆盖，例如 `coe` 覆盖到 chiwei-test 基础设施 |
| `lane_overrides` | 按具体 lane 覆盖，例如 `coe-exp1` |
| `required_keys` | 部署某类 lane 前强制要求该 class 覆盖的 key |
| `referenced_by` | 查询时由服务端填充，表示哪些 App 引用了该 bundle |

合并顺序为：

`keys` < `class_overrides[class]` < `lane_overrides[lane]`

App 可以引用多个 ConfigBundle，但不同 bundle 的 `keys` 不允许定义同名 key。若确实需要覆盖某个 key，应使用 class/lane override、App envs 或 Release envs，而不是在多个 bundle 里重复定义。

## lane class 覆盖

lane 命名决定 class：

| lane | class | 配置含义 |
|---|---|---|
| `prod` | `prod` | 生产基线 |
| `blue` | `prod` | paas-engine 蓝绿自部署专用，连接 prod 基础设施 |
| `ppe-<name>` | `ppe` | 共用 prod 基础设施 |
| `coe-<name>` | `coe` | 独立 chiwei-test 基础设施 |

`coe-*` 的 DB/Redis/MQ/Qdrant 等隔离应通过 `class_overrides["coe"]` 完成，业务代码不应自己按 lane 拼不同连接串。

如果某个 bundle 声明了：

```json
{
  "required_keys": {
    "coe": ["POSTGRES_HOST", "POSTGRES_DB"]
  }
}
```

部署 `coe-*` lane 前，服务端会校验 `class_overrides["coe"]` 中这些 key 非空；缺失则拒绝发布。

## App envs

App envs 是 App 级稳定环境变量，适合不会按 lane 频繁变动的运行参数。更新入口：

```bash
curl -sf -X PUT "$PAAS_API/api/paas/apps/<app>/" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $PAAS_TOKEN" \
  -d '{"envs":{"KEY":"value"}}'
```

`PUT /apps/{app}/` 是字段级 merge；其中 `envs` 又是 key 级 merge：

| 请求片段 | 结果 |
|---|---|
| 未发送 `envs` | 保持不变 |
| `"envs": {}` | 保持不变 |
| `"envs": {"K":"V"}` | 新增或覆盖 `K` |
| `"envs": {"K": null}` | 删除 `K` |
| `"envs": null` | 清空整个 App envs |

## Release envs

Release envs 是 App + lane 级覆盖，适合临时验证或某条 lane 的特殊值。创建 Release 时可带：

```json
{
  "app_name": "agent-service",
  "lane": "coe-exp1",
  "image_tag": "1.2.3.4",
  "replicas": 1,
  "envs": {
    "KEY": "value"
  }
}
```

更新已有 Release 时，`PUT /api/paas/releases/{id}/` 的 `envs` 使用同样的 key 级 merge 语义。

`make deploy` 和 `make release` 不暴露 `envs` 参数；需要 Release envs 时直接调用 PaaS API。

## 最终优先级

App 在某条 lane 的最终环境变量优先级为：

`envFrom 引用` < `ConfigBundle keys` < `ConfigBundle class_overrides` < `ConfigBundle lane_overrides` < `App envs` < `Release envs` < 自动注入

自动注入包括：

| 变量 | 值 |
|---|---|
| `LANE` | 当前 release lane |
| `APP_NAME` | App 名 |
| `VERSION` | Release version，存在时注入 |

实现上，ConfigBundle 解析结果会写入 PaaS 自动管理的 K8s Secret：`{app}-{lane}-config`，再通过 `envFrom` 注入容器。这个 Secret 是结果态，不是人工维护入口。

## 查看最终配置

查看 App 在某 lane 的最终配置：

```bash
curl -sf "$PAAS_API/api/paas/apps/<app>/resolved-config?lane=<lane>" \
  -H "X-API-Key: $PAAS_TOKEN"
```

返回值会标注来源，例如 bundle 名、`[class:coe]`、`[lane:coe-exp1]`、`app`、`release`、`auto`。

不要复制生产配置值。排查时只记录 key 名、来源和是否为空。

## Dynamic Config

Dynamic Config 只用于业务行为参数，不用于基础设施连接。读取规则：

- `prod` 是基线。
- 非 prod lane 只存覆盖值。
- 读取时合并：当前 lane 覆盖 > prod 基线。
- SDK 缓存 10 秒。

管理 API：

| 用途 | 端点 |
|---|---|
| SDK 读取 | `GET /internal/dynamic-config/resolved?lane=<lane>` |
| 管理查询 | `GET /api/paas/dynamic-config/` |
| 管理设置 | `PUT /api/paas/dynamic-config/{key}` body: `{"lane":"prod","value":"..."}` |
| 管理删除 | `DELETE /api/paas/dynamic-config/{key}?lane=<lane>` |

Python 服务如果需要 lane-aware 读取，不要直接依赖默认 `inner_shared.dynamic_config.dynamic_config` 单例；默认单例没有 lane provider，会 fallback 到 `prod`。应在服务内创建带 lane provider 的实例：

```python
import os

from inner_shared.dynamic_config import DynamicConfig
from app.api.middleware import get_lane

dynamic_config = DynamicConfig(
    paas_engine_url=os.getenv("PAAS_ENGINE_URL", "http://paas-engine:8080"),
    lane_provider=get_lane,
)
```

TypeScript 默认 `new DynamicConfig()` 会从共享 context 读取 lane；需要特殊上下文时显式传 `laneProvider`。

## PaaS Engine 自身环境变量

paas-engine 也是一个 App。它自己的进程环境变量仍通过 PaaS 管理；`paas-engine-secret` 等 K8s Secret 只作为初始化凭证资源，不作为日常变更入口。

paas-engine 读取这些变量：

| 变量 | 默认值/说明 |
|---|---|
| `HTTP_PORT` | 默认 `8080` |
| `DATABASE_URL` | paas-engine 元数据库连接 |
| `KUBECONFIG` | 集群外运行时可用；集群内通常为空 |
| `DEPLOY_NAMESPACE` | 默认 `default` |
| `KANIKO_NAMESPACE` | 默认 `paas-builds` |
| `KANIKO_IMAGE` | 默认 `harbor.local:30002/inner-bot/kaniko:latest` |
| `REGISTRY_SECRET` | 默认 `harbor-secret` |
| `REGISTRY_MIRRORS` | CSV |
| `INSECURE_REGISTRIES` | CSV |
| `REGISTRY_BASE` | 默认 `registry.example.com` |
| `KANIKO_CACHE_REPO` | 空则禁用远程层缓存 |
| `KANIKO_GIT_AUTH_SECRET_PREFIX` | 默认 `kaniko-git-auth`。构建克隆用的 git 凭据 Secret 名为 `<前缀>-<LANE>`，建在 `KANIKO_NAMESPACE` 里，见下方「构建克隆用的 GitHub token」 |
| `BUILD_HTTP_PROXY` | 构建 Pod 代理 |
| `BUILD_NO_PROXY` | 构建 Pod no_proxy |
| `API_TOKEN` | PaaS API token |
| `LOKI_URL` | 默认 `http://loki-gateway.monitoring.svc.cluster.local` |
| `CHIWEI_DATABASE_URL` | 业务库 ops 查询 |
| `CHIWEI_TEST_DATABASE_URL` | chiwei-test ops 查询 |
| `SIDECAR_IMAGE` | lane sidecar 镜像 |
| `CI_NAMESPACE` | 默认 `paas-builds` |
| `CI_GIT_REPO` | Git poller 仓库 |
| `GITHUB_TOKEN` | App env。Git poller 用它轮询分支；kaniko 构建用它认证克隆。为空时构建匿名克隆 |
| `GIT_POLL_INTERVAL` | 默认 `60s` |
| `LEGACY_LANE_WHITELIST` | CSV，历史 lane 兼容白名单 |
| `LANE` | 自动注入，本实例所在泳道。决定构建 git 凭据 Secret 的名字；为空时构建不带凭据 |

### 构建克隆用的 GitHub token

kaniko 构建 Job 克隆代码时用 `GITHUB_TOKEN` 认证（`GIT_USERNAME=x-access-token`、`GIT_PASSWORD=<token>`）。匿名克隆走共享代理，出口 IP 上有大量别人的匿名 GitHub 流量，被限流时 GitHub 对公开仓库也回 401 `Repository not found`；认证后走 token 自己的额度。

token 的唯一配置入口是 paas-engine 的 App env。每个 paas-engine 实例（prod、blue、任意 ppe）在每次提交构建时，把自己启动时读到的 token 写进 `KANIKO_NAMESPACE` 里名为 `<KANIKO_GIT_AUTH_SECRET_PREFIX>-<LANE>` 的 Secret（例如 `kaniko-git-auth-prod`），它创建的 Job 只引用自己这一份。这些 Secret 是结果态，不要手改，也不要手工另建一份。本次写入失败时（例如 paas-builds 的 Role 缺 secrets 权限），这次构建不带凭据、按匿名克隆，paas-engine 日志里有一条 `sync git auth secret failed` 告警，点名 Secret 和错误。

轮换 token 的正常路径是**先换上新 token，再撤销旧的**。重新部署 paas-engine 本身要构建，而这次构建是由还持有旧 token 的旧进程提交的，所以旧 token 在这一步必须仍然有效：

1. 在 GitHub 上生成新 token，旧 token 先不撤销。
2. 改 App env：`PUT /api/paas/apps/paas-engine/`，body `{"envs":{"GITHUB_TOKEN":"<新 token>"}}`。
3. 重新部署所有存活的 paas-engine 实例：`make self-deploy GIT_REF=main` 同时更新 prod 和 blue；有 ppe 实例的也要重新部署或下掉。token 只在进程启动时读一次，没重新部署的实例会一直写旧 token。
4. 重新部署后，每个实例在它的下一次构建时把新 token 写进自己的 Secret。确认新进程提交的构建成功之后，再到 GitHub 上撤销旧 token。

**旧 token 已经失效（过期或已撤销）时不能走 self-deploy**：所有构建都会 401，包括构建 paas-engine 自己的那一次。恢复方法是改完 App env（上面第 2 步）后不构建，直接用各泳道当前的镜像重新发布 paas-engine。App env 变了，Pod 模板跟着变，Deployment 会滚出读到新 token 的新 Pod：

```bash
make status APP=paas-engine                                     # 每条泳道当前的镜像，tag 是最后一个冒号后面那段
make release APP=paas-engine LANE=prod VERSION=<prod 当前 tag>
make release APP=paas-engine LANE=blue VERSION=<blue 当前 tag>   # 只在 blue 有 Release 时
```

ppe 上的 paas-engine 同样用 `make release APP=paas-engine LANE=ppe-<name> VERSION=<该泳道当前 tag>`，或者直接下掉。`make release` 的请求不带 envs，会清空该 Release 已有的 Release envs，self-deploy 也是这样，所以 paas-engine 的 Release 上本来就不该放需要保留的 Release envs。如果确实有，改用 `PUT /api/paas/releases/{id}/`、body `{}`，它用当前镜像重新发布，Release envs 保持不变。

从 App env 删掉 `GITHUB_TOKEN` 后，重新部署的实例不再引用也不再写 Secret。已下掉的实例留下的 Secret 不会被自动删除（paas-engine 没有 secrets 的 delete 权限），里面是该实例最后持有的 token，在 GitHub 上撤销后就失效了。

排查：**全部构建都 401 `Repository not found`，先查 token 是否过期或被撤销。** token 失效时 GitHub 对公开仓库也回 401，go-git 不会退回匿名，报错文字和匿名限流时一样。确认失效后按上面「旧 token 已经失效」的方法恢复。只有零星构建 401、其余成功，才像匿名限流；这时用 `make logs APP=paas-engine KEYWORD="sync git auth secret failed"` 看那次构建是不是因为同步失败退回了匿名克隆。

## 变更流程

1. 判断配置类型：基础设施/密钥用 ConfigBundle；App 稳定参数用 App envs；lane 临时覆盖用 Release envs；业务行为参数用 Dynamic Config。
2. 通过 PaaS API 修改源头配置。
3. 若是部署时配置，重新发布目标 App/lane；一镜像多服务要同步 sibling release。
4. 用 `resolved-config` 验证来源和值是否符合预期；不要输出真实密钥。
5. 若影响真实流量，按项目部署和验证规范走泳道验证。

## 常见误区

- 不要用 `kubectl edit secret` 或手改 ConfigMap 配环境变量；那只是结果态或初始化资源。
- 不要把业务 feature flag 放进 ConfigBundle；它会变成部署时配置，不能 10 秒内生效。
- 不要让业务代码按 lane 自己选择 DB/Redis/MQ；隔离由 ConfigBundle class override 负责。
- 不要为了一个 lane 的临时值改 App envs；优先用 Release envs 或 Dynamic Config。
- 不要把真实 DSN/API key 写进文档、测试、commit message 或协作记录。
