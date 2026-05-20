---
name: ops-db
description: 安全查询 PaaS Engine PostgreSQL 数据库，以及提交 DDL/DML 变更申请
user_invocable: true
---

# /ops-db

查询 PostgreSQL 数据库（只读），或提交 DDL/DML 变更申请（需人工审批后执行）。

## 用法

### 只读查询

```
/ops-db @chiwei <SQL>            # 查询 chiwei（业务数据）
/ops-db @chiwei-test <SQL>       # 查询 chiwei-test（COE 隔离离线库）
/ops-db @paas_engine <SQL>       # 查询 paas_engine（PaaS 元数据）
/ops-db @chiwei schema           # 查看 chiwei 的表结构
/ops-db @paas_engine schema      # 查看 paas_engine 的表结构
```

**`@数据库` 必填，不传会报错。**

### 提交变更申请

```
/ops-db submit @chiwei ALTER TABLE messages ADD COLUMN foo TEXT;
-- reason: 支持新字段 foo 存储 xxx
```

- `@数据库` 必填，指定目标库
- `-- reason:` 说明变更目的（强烈建议填写）
- 提交后返回 `mutation_id`，状态为 `pending`
- **告知用户**：已提交审批，ID=<id>，请前往 Dashboard → DB 变更 页面审批

### 提交变更申请（大文本 / 含特殊字符，走文件入口）

当 SQL 含 PL/pgSQL `DO $$ ... $$`、`$var`、单双引号、`%`、换行，或要复刻
persona 这类大文本时，**不要**走命令行（`$ARGUMENTS` 经 shell 会把 `$$`
展开成进程 PID，SQL 损坏）。改用 `--file` 入口，SQL 从文件逐字节原样读取，
完全不经 shell：

```
python3 .claude/skills/ops-db/query.py submit @chiwei-test --file /tmp/seed.sql --reason "复刻 prod bot_persona 到 chiwei-test"
```

- 先把 SQL 写入纯路径文件（如 `/tmp/seed.sql`，路径不含特殊字符）
- `--file <path>`：从该文件按 UTF-8 逐字节读取 SQL，原样提交，不做任何处理
- `--reason <text>`：变更说明用独立参数传，**不再**从 SQL 里正则切
  `-- reason:`（避免 persona 文本里的同形子串被误当成 reason）
- `@数据库`、返回格式、审批流程与上面的命令行 submit 完全一致
- 命令行 `submit @db <SQL> -- reason:` 旧用法保持不变，仅在需要文件入口时使用 `--file`

### 查询审批状态

```
/ops-db status 42
```

返回当前状态（pending/approved/rejected/failed）、审批备注、执行时间或错误信息。

## 数据库选择指引

| 库名 | 用途 | 何时使用 |
|------|------|---------|
| `paas_engine` | PaaS 元数据：apps、builds、releases、config_bundles 等表 | 操作 PaaS 管理数据 |
| `chiwei` | 业务数据：messages、chats、users、memory 等表 | 操作赤尾业务数据 |
| `chiwei-test` | COE 隔离的独立离线 PG 容器集（chiwei_test 库） | 操作 coe-* 泳道测试数据，与线上隔离 |

**提交前**先用 `@数据库 schema` 确认目标库和表结构正确。

## 预处理数据

```
!`python3 .claude/skills/ops-db/query.py "$ARGUMENTS"`
```

## 指令

1. 如果是只读查询结果（`columns` + `rows`），格式化为 markdown 表格输出
2. 如果是 `submit` 结果（`id` + `status: pending`），输出：
   ```
   已提交 DDL/DML 审批申请：
   - Mutation ID: <id>
   - 数据库: <db>
   - 状态: pending（等待人工审批）
   请前往 Dashboard → DB 变更 页面完成审批。
   ```
3. 如果是 `status` 结果，格式化状态信息（状态、审批人、备注、执行时间、错误）
4. 如果预处理报错，直接展示错误信息
