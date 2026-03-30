# Unified Config System (ConfigBundle) 设计文档

> 日期: 2026-03-30
> 状态: Approved

## 背景与动机

当前 PaaS 平台的配置管理存在三个割裂的层面：

| 层面 | 管理方式 | 问题 |
|------|---------|------|
| App.Envs | PaaS API 可读写 | 明文存储，不能按泳道覆盖，跨 app 无法复用 |
| Release.Envs | PaaS API 可读写 | 仅覆盖用途，生命周期绑定 Release |
| EnvFromSecrets / ConfigMaps | 仅存名称引用 | PaaS 无法管理内容，黑盒，需要 kubectl 手动操作 |

核心痛点：
1. **复用差** — 同一 DB 密码 5 个 app 各挂一遍，改一次改 5 处
2. **App envs 不能按泳道覆盖** — dev 想连不同 DB 做不到
3. **Secret 是黑盒** — PaaS 管不了 K8s Secret 内容
4. **命名不统一** — 同一个 Redis，tool-service 叫 `TOOL_REDIS_HOST`，其他 app 叫 `REDIS_HOST`
5. **主要消费者是 Claude** — 操作体验优先，一套 API 搞定一切

## 设计目标

- 所有配置通过 PaaS API 统一管理，消除 kubectl 手动操作
- 按基础设施实例分组（ConfigBundle），一处修改、多 app 生效
- 泳道按 key 粒度覆盖，继承基线值
- 统一命名规范 `{BUNDLE}_{FIELD}`，消除跨 app 命名混乱
- 支持自动生成随机值（密码、token）
- 平滑迁移，不停机

## 数据模型

### ConfigBundle（配置包）

```go
type ConfigBundle struct {
    Name          string                       // "pg-main", "redis", "rabbitmq"
    Description   string                       // "主 PostgreSQL 数据库"
    Keys          []ConfigKey                  // 配置项列表
    LaneOverrides map[string]map[string]string // lane → {key: value}
}

type ConfigKey struct {
    Name     string // 最终的环境变量名，如 "PG_MAIN_HOST"
    Value    string // 基线值（DB 层 AES-256-GCM 加密）
    Generate string // 可选："random:32", "random:hex:16" → 创建时自动生成
}
```

### App 引用方式

```go
type App struct {
    Name          string
    ConfigBundles []string          // ["pg-main", "redis", "rabbitmq"]
    Envs          map[string]string // app 专属配置（过渡期保留）
    // EnvFromSecrets / EnvFromConfigMaps → 迁移完成后废弃
}
```

### 注入优先级（低 → 高）

```
ConfigBundle baseline → ConfigBundle lane override → App.Envs → Release.Envs
```

后者覆盖前者。自动注入 `LANE` 和 `VERSION`。

### 数据库表结构

**config_bundles 表：**
```sql
CREATE TABLE config_bundles (
    name        VARCHAR(63) PRIMARY KEY,
    description TEXT,
    created_at  TIMESTAMP,
    updated_at  TIMESTAMP
);
```

**config_keys 表：**
```sql
CREATE TABLE config_keys (
    id          SERIAL PRIMARY KEY,
    bundle_name VARCHAR(63) REFERENCES config_bundles(name),
    name        VARCHAR(255) NOT NULL,    -- 环境变量名
    value       BYTEA NOT NULL,           -- AES-256-GCM 加密
    generate    VARCHAR(50),              -- 自动生成规则
    created_at  TIMESTAMP,
    updated_at  TIMESTAMP,
    UNIQUE(bundle_name, name)
);
```

**config_lane_overrides 表：**
```sql
CREATE TABLE config_lane_overrides (
    id          SERIAL PRIMARY KEY,
    bundle_name VARCHAR(63) REFERENCES config_bundles(name),
    lane        VARCHAR(63) NOT NULL,
    key_name    VARCHAR(255) NOT NULL,
    value       BYTEA NOT NULL,           -- AES-256-GCM 加密
    created_at  TIMESTAMP,
    updated_at  TIMESTAMP,
    UNIQUE(bundle_name, lane, key_name)
);
```

## API 设计

### ConfigBundle CRUD

```
POST   /api/paas/config-bundles/                    创建配置包
GET    /api/paas/config-bundles/                    列出所有配置包
GET    /api/paas/config-bundles/{name}              获取配置包（含 referenced_by）
PUT    /api/paas/config-bundles/{name}              更新配置包（merge 语义）
DELETE /api/paas/config-bundles/{name}              删除配置包（需无 app 引用）
```

### Key 管理

```
PUT    /api/paas/config-bundles/{name}/keys         批量设置 keys（merge 语义）
DELETE /api/paas/config-bundles/{name}/keys/{key}   删除单个 key
POST   /api/paas/config-bundles/{name}/keys/{key}/generate   自动生成随机值
```

### 泳道覆盖

```
PUT    /api/paas/config-bundles/{name}/lanes/{lane}          设置泳道覆盖（merge 语义）
DELETE /api/paas/config-bundles/{name}/lanes/{lane}          删除整个泳道覆盖
DELETE /api/paas/config-bundles/{name}/lanes/{lane}/{key}    删除单个 key 的泳道覆盖
```

### App 绑定

```
PUT    /api/paas/apps/{app}/    新增 config_bundles 字段
```

### 最终配置查询

```
GET    /api/paas/apps/{app}/resolved-config?lane=dev
```

返回合并后的完整环境变量，带来源标注：

```json
{
    "PG_MAIN_HOST":  {"value": "dev-postgres", "source": "pg-main[lane:dev]"},
    "PG_MAIN_PORT":  {"value": "5432",         "source": "pg-main"},
    "REDIS_HOST":    {"value": "redis",        "source": "redis"},
    "DEBUG":         {"value": "true",         "source": "release"}
}
```

### 操作语义示例

**创建配置包：**
```json
POST /api/paas/config-bundles/
{
    "name": "pg-main",
    "description": "主 PostgreSQL 数据库",
    "keys": [
        {"name": "PG_MAIN_HOST", "value": "postgres"},
        {"name": "PG_MAIN_PORT", "value": "5432"},
        {"name": "PG_MAIN_USER", "value": "chiwei"},
        {"name": "PG_MAIN_PASSWORD", "value": "xxx", "generate": "random:32"},
        {"name": "PG_MAIN_DATABASE", "value": "chiwei"}
    ]
}
```

**泳道覆盖：**
```json
PUT /api/paas/config-bundles/pg-main/lanes/dev
{
    "PG_MAIN_HOST": "dev-postgres",
    "PG_MAIN_DATABASE": "chiwei_dev"
}
```

**App 绑定：**
```json
PUT /api/paas/apps/agent-service/
{
    "config_bundles": ["pg-main", "redis", "rabbitmq", "search-apis"]
}
```

### 冲突检测

App 绑定多个 bundle 时，如果两个 bundle 定义了同名 key，绑定时报错。强制包的 key 命名带前缀，从源头避免冲突。

### 影响分析

`GET /api/paas/config-bundles/{name}` 响应包含 `referenced_by` 字段：

```json
{
    "name": "pg-main",
    "referenced_by": ["agent-service", "lark-server", "tool-service"],
    "keys": [...]
}
```

## 部署注入流程

### 注入逻辑

```go
// 1. resolve 所有 key（bundle → lane override → app.envs → release.envs）
resolved := resolveConfig(app, release)

// 2. 全部写入一个自动管理的 K8s Secret
secretName := fmt.Sprintf("%s-%s-config", app.Name, lane)
createOrUpdateSecret(secretName, resolved)

// 3. container 只需 envFrom
container := Container{
    EnvFrom: []EnvFromSource{{SecretRef: secretName}},
}
```

不区分 sensitive / non-sensitive，所有值统一走 K8s Secret。原因：
- 主要消费者是 Claude，通过 `resolved-config` API 查看，不依赖 `kubectl describe`
- 简化 deployer 逻辑，无需拆分

### Secret 生命周期

- 命名：`{app}-{lane}-config`，如 `agent-service-prod-config`
- PaaS 全权管理创建、更新、删除
- 部署时自动同步，Release 删除时对应 Secret 一并清理

## 迁移策略

### Phase 1：建表 + API + 读取黑盒

- 新增 `config_bundles`、`config_keys`、`config_lane_overrides` 三张表
- 实现 ConfigBundle CRUD API + resolved-config
- 读取 `app-env`、`main-server-config`、`ai-service-config` 的实际内容，确定完整的 bundle 拆分方案

### Phase 2：数据迁移（双写）

- 把现有配置写入 ConfigBundle
- Deployer 同时读旧字段和新 ConfigBundle，合并注入
- 注入顺序：`旧 envFrom` → `旧 App.Envs` → `ConfigBundle resolved` → `Release.Envs`
- 逐个 app 部署验证，`resolved-config` 对比实际 pod env

### Phase 3：切换（逐 app）

- 确认某个 app 的 ConfigBundle 配置完整后，清空 `env_from_secrets`、`env_from_config_maps`、`envs` 中已迁移的 key
- 部署验证
- 逐个 app 完成

### Phase 4：清理

- 所有 app 迁移完毕后，废弃旧字段
- 清理不再需要的手工 K8s Secret/ConfigMap

### 风险控制

- 不删旧数据 — Phase 3 之前旧字段原样保留
- resolved-config 对比验证 — diff 为空才算迁移成功
- 逐 app 切换 — 一个 app 出问题不影响其他

## 统一命名规范

所有 ConfigBundle key 采用 `{BUNDLE}_{FIELD}` 格式，大写下划线：

| Bundle | Keys |
|--------|------|
| **pg-main** | `PG_MAIN_HOST`, `PG_MAIN_PORT`, `PG_MAIN_USER`, `PG_MAIN_PASSWORD`, `PG_MAIN_DATABASE` |
| **redis** | `REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD` |
| **rabbitmq** | `RABBITMQ_HOST`, `RABBITMQ_PORT`, `RABBITMQ_USER`, `RABBITMQ_PASSWORD`, `RABBITMQ_VHOST` |
| **lark** | `LARK_APP_ID`, `LARK_APP_SECRET`, `LARK_ENCRYPT_KEY`, `LARK_VERIFICATION_TOKEN` |
| **forward-proxy** | `FORWARD_PROXY_URL` |
| **search-apis** | `SEARCH_GOOGLE_API_KEY`, `SEARCH_GOOGLE_CX`, `SEARCH_GOOGLE_HOST`, `SEARCH_SERPAPI_KEY`, `SEARCH_YOU_API_KEY`, `SEARCH_YOU_HOST`, `SEARCH_SILICONFLOW_KEY` |
| **tos** | `TOS_ACCESS_KEY_ID`, `TOS_ACCESS_KEY_SECRET`, `TOS_BUCKET`, `TOS_ENDPOINT`, `TOS_REGION` |
| **paas-internal** | `PAAS_TOKEN`, `PAAS_GITHUB_TOKEN`, `PAAS_CI_GIT_REPO`, `PAAS_BUILD_HTTP_PROXY`, `PAAS_BUILD_NO_PROXY`, `PAAS_DEPLOY_NAMESPACE`, `PAAS_INSECURE_REGISTRIES`, `PAAS_KANIKO_CACHE_REPO`, `PAAS_KANIKO_IMAGE`, `PAAS_REGISTRY_MIRRORS`, `PAAS_DATABASE_URL` |
| **inter-service-auth** | `AUTH_INNER_HTTP_SECRET`, `AUTH_PROXY_HTTP_SECRET` |

### App 侧改造

各 app 代码中读取环境变量的地方需对齐新命名：

| App | 现在读 | 改为读 |
|-----|--------|--------|
| tool-service | `TOOL_DATABASE_URL` | 用 `PG_MAIN_*` 各字段拼接 |
| tool-service | `TOOL_REDIS_HOST` | `REDIS_HOST` |
| tool-service | `TOOL_TOS_ACCESS_KEY_ID` | `TOS_ACCESS_KEY_ID` |
| agent-service | `GOOGLE_SEARCH_API_KEY` | `SEARCH_GOOGLE_API_KEY` |
| agent-service | `POSTGRES_PORT` | `PG_MAIN_PORT` |
| sandbox-worker | `INNER_HTTP_SECRET` | `AUTH_INNER_HTTP_SECRET` |

### 连接字符串

Bundle 只存分拆字段（HOST/PORT/USER/PASSWORD/DATABASE）。需要连接字符串的 app 在代码启动时自行拼接。

## 不在 v1 范围内

- 配置模板语法（如 `PG_MAIN_URL = "postgres://${PG_MAIN_USER}:${PG_MAIN_PASSWORD}@${PG_MAIN_HOST}:${PG_MAIN_PORT}/${PG_MAIN_DATABASE}"`）
- 配置变更自动触发重部署
- 配置版本历史 / 回滚
- Web UI
