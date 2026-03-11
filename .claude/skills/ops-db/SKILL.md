---
description: 安全查询 PaaS Engine PostgreSQL 数据库
user_invocable: true
---

# /ops-db

安全只读查询 PostgreSQL 数据库。

## 用法

```
/ops-db <SQL>                    # 查询 paas_engine（默认）
/ops-db @chiwei <SQL>            # 查询 chiwei
/ops-db @paas-engine <SQL>       # 查询 paas_engine（显式指定）
/ops-db schema                   # 查看 paas_engine 的表结构
/ops-db @chiwei schema           # 查看 chiwei 的表结构
```

## 预处理数据

```
!`python3 .claude/skills/ops-db/query.py "$ARGUMENTS"`
```

## 指令

1. 上面的预处理结果是 JSON 格式的查询结果（`columns` + `rows`），将其格式化为 markdown 表格输出
2. 如果预处理报错，直接展示错误信息
