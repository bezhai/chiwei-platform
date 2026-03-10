---
description: 安全查询 PaaS Engine PostgreSQL 数据库
user_invocable: true
---

# /ops-db

安全查询 PaaS Engine 的 PostgreSQL 数据库。

## 预处理数据

```
!`python3 .claude/skills/ops-db/query.py $ARGUMENTS`
```

## 指令

1. 上面的预处理结果是 JSON 格式的查询结果（`columns` + `rows`），将其格式化为 markdown 表格输出
2. 如果预处理报错，直接展示错误信息
