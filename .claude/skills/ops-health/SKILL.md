---
description: 一键检查所有服务健康端点
user_invocable: true
---

# /ops-health

检查所有业务服务的健康状态。

## 预处理数据

```
!`bash .claude/skills/ops-health/check.sh`
```

## 指令

1. 解析上面的健康检查结果，输出格式化的状态表：

   | 服务 | 端口 | 状态 |
   |------|------|------|
   | xxx  | 8000 | OK / DOWN / TIMEOUT |

2. 200 = OK，000 = TIMEOUT（无法连接），其他 = 异常（标注 HTTP 状态码）
3. 如有异常服务，给出排查建议（如检查 pod 状态、查看日志等）
4. 如果 `/healthz` 返回 404，提示该服务可能使用其他健康检查路径（如 `/health`、`/api/health`）
