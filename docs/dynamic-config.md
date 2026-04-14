# 动态配置系统

业务行为参数（模型选择、阈值、feature flag 等）的运行时配置。修改后无需重新部署，10 秒内生效。

基础设施连接（DB/Redis 连接串）仍走 ConfigBundle（部署时环境变量）。

## 概念

- **全局配置空间**，唯一隔离维度是泳道
- `prod` 是基线值，其他泳道是覆盖值
- 读取时自动合并：泳道覆盖 > prod 基线，无覆盖则 fallback 到 prod
- SDK 从 request context 自动获取当前泳道，业务代码无需感知

## 管理

Dashboard「动态配置」页面，支持：

- 切换泳道查看合并后的生效配置
- 新增 / 编辑配置值
- 非 prod 泳道可「恢复到 prod」（删除覆盖）

## 服务接入

### Python（agent-service 等）

```python
# app/infra/dynamic_config.py — 创建一次，全局使用
from inner_shared.dynamic_config import DynamicConfig
from app.api.middleware import get_lane

dynamic_config = DynamicConfig(lane_provider=get_lane)
```

```python
# 业务代码中
from app.infra.dynamic_config import dynamic_config

model = dynamic_config.get("default_model", default="gemini")
threshold = dynamic_config.get_float("proactive_threshold", default=0.7)
enabled = dynamic_config.get_bool("feature_x_enabled", default=False)
count = dynamic_config.get_int("max_retry", default=3)
```

### TypeScript（lark-server 等）

```typescript
// src/infrastructure/dynamic-config.ts — 创建一次，全局使用
import { DynamicConfig } from 'ts-shared';

export const dynamicConfig = new DynamicConfig();
```

```typescript
// 业务代码中（注意 async）
import { dynamicConfig } from '../infrastructure/dynamic-config';

const model = await dynamicConfig.get("default_model", "gemini");
const threshold = await dynamicConfig.getFloat("proactive_threshold", 0.7);
const enabled = await dynamicConfig.getBool("feature_x_enabled", false);
```

## API

| 用途 | 端点 | 认证 |
|------|------|------|
| SDK 读取 | `GET /internal/dynamic-config/resolved?lane=xxx` | 无（集群内部） |
| 管理查询 | `GET /api/paas/dynamic-config/` | API Token |
| 管理设置 | `PUT /api/paas/dynamic-config/{key}` body: `{"lane":"prod","value":"..."}` | API Token |
| 管理删除 | `DELETE /api/paas/dynamic-config/{key}?lane=xxx` | API Token |

## 数据存储

```sql
-- paas-engine PostgreSQL
CREATE TABLE dynamic_configs (
    key        TEXT      NOT NULL,
    lane       TEXT      NOT NULL DEFAULT 'prod',
    value      TEXT      NOT NULL,
    updated_at TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (key, lane)
);
```
