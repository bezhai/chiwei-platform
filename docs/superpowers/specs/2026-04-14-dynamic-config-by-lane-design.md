# 动态配置系统（按泳道隔离）

## 问题

当前 ConfigBundle 是部署时静态下发环境变量，不支持运行时热更新。业务行为参数（模型选择、阈值、feature flag 等）修改后需要重新部署才能生效。同时，当 prod Pod 通过 fallback 处理其他泳道的请求时，无法读到该泳道的配置。

系统普遍不了解 ConfigBundle，且 ConfigBundle 解决的是基础设施连接问题，不适合承载业务行为参数。

## 设计目标

- 业务行为参数的运行时动态配置，修改后无需重新部署
- 按泳道隔离，fallback 到 prod
- SDK 透明接入，业务代码只调 `get("key", default=...)`，lane 从 context 自动获取
- Dashboard 页面按泳道管理配置

## 数据模型

```sql
CREATE TABLE dynamic_configs (
    key        TEXT      NOT NULL,
    lane       TEXT      NOT NULL DEFAULT 'prod',
    value      TEXT      NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (key, lane)
);
```

- `lane='prod'` 是基线值，其他 lane 是覆盖值
- 删除某 lane 的覆盖 = 该 lane fallback 到 prod
- value 统一 TEXT，类型转换由 SDK 负责
- 无"注册"概念，直接写 key-value 即存在

## API 设计

扩展 paas-engine，新增路由组 `/api/paas/dynamic-config/`。

### 读取（SDK 用）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/resolved?lane=dev` | 返回合并后的全量快照（lane 覆盖 + prod 补缺） |

响应：

```json
{
  "configs": {
    "default_model": {"value": "gemini", "lane": "prod"},
    "proactive_threshold": {"value": "0.5", "lane": "dev"}
  },
  "resolved_at": "2026-04-14T10:00:00Z"
}
```

每个值带 `lane` 字段标注来源，方便调试。

### 管理（Dashboard 用）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/` | 列出所有配置（支持 `?lane=xxx` 筛选） |
| PUT | `/{key}` | 设置值，body: `{"lane": "prod", "value": "gemini"}`，upsert 语义 |
| DELETE | `/{key}?lane=dev` | 删除某 lane 的覆盖（fallback 到 prod） |
| DELETE | `/{key}` | 删除所有 lane 的该 key（不传 lane） |

## SDK 设计

Python（`packages/py-shared`）和 TypeScript（`packages/ts-shared`）各一个，行为一致。

### 使用方式

```python
from inner_shared.dynamic_config import dynamic_config

model = dynamic_config.get("default_model", default="gemini")
threshold = dynamic_config.get_float("proactive_threshold", default=0.7)
enabled = dynamic_config.get_bool("feature_x_enabled", default=False)
count = dynamic_config.get_int("max_retry", default=3)
```

```typescript
import { dynamicConfig } from 'ts-shared/dynamic-config'

const model = dynamicConfig.get("default_model", "gemini")
const threshold = dynamicConfig.getFloat("proactive_threshold", 0.7)
```

### 内部机制

1. **缓存结构**：`dict[lane, (snapshot, expire_time)]`，每个 lane 一份快照
2. **读取流程**：
   - 从 context 取 lane（取不到则为 `"prod"`）
   - 查缓存，未过期则直接读
   - 过期则调 `GET /api/paas/dynamic-config/resolved?lane={lane}`，刷新缓存
   - key 不在 snapshot 中则返回 default
3. **缓存 TTL**：10 秒，过期后下次读触发刷新（同步，lazy）
4. **初始化**：无需显式初始化，首次调用时自动连接 paas-engine（地址通过 LaneRouter 解析）
5. **类型方法**：`get` 返回 str，`get_int` / `get_float` / `get_bool` 做转换，转换失败返回 default

## Dashboard 页面

在现有 paas-engine Dashboard 中新增「动态配置」页面。

### 布局

- 顶部：泳道选择器（下拉，默认 prod）
- 主体：表格，列为 Key | Value | 来源(prod/当前 lane) | 操作
- 操作列：编辑、删除覆盖（非 prod lane 时显示"恢复到 prod"）
- 表格上方：「新增配置」按钮

### 交互

- 选 prod：直接编辑基线值
- 选其他泳道：显示合并结果，来源列标注继承自 prod 还是本 lane 覆盖；编辑时创建该 lane 的覆盖；"恢复到 prod" 删除覆盖
- 新增：输入 key + value，写入当前选中的 lane

## 与现有系统的关系

| 系统 | 职责 | 生效时机 |
|------|------|---------|
| ConfigBundle | 基础设施连接（DB/Redis/MQ 连接串） | 部署时，环境变量 |
| Dynamic Config | 业务行为参数（模型/阈值/flag） | 运行时，SDK 读取，10s 缓存 |

两者正交，互不干扰。
