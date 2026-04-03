# DDL/DML 人工审批执行支持

**日期**: 2026-04-03  
**状态**: 已确认，待实现

---

## 背景

当前 `ops-db` skill 只支持只读 SELECT 查询，DDL/DML 操作被服务端正则表达式拦截（HTTP 403）。  
需要让 Claude 能够提交 DDL/DML 申请，经人工在 Dashboard 审批后自动执行。

---

## 目标

1. Claude 通过 `ops-db` skill 提交 DDL/DML 申请（附说明）
2. 人工在 Dashboard "DB 变更" 页面审批
3. 审批通过后后端立即执行 SQL，结果记录在案
4. 拒绝时可填写原因，Claude 可查询状态

---

## 数据模型

paas-engine 新增 `db_mutations` 表，加入 `AutoMigrate` 列表：

```go
type DbMutationModel struct {
    ID          uint       `gorm:"primaryKey;autoIncrement"`
    DB          string     `gorm:"not null"`                    // 目标库: paas_engine / chiwei
    SQL         string     `gorm:"not null;type:text"`          // 提交的 SQL
    Reason      string     `gorm:"type:text"`                   // 提交说明
    Status      string     `gorm:"not null;default:'pending'"`  // pending/approved/rejected/failed
    SubmittedBy string     `gorm:"not null"`                    // 提交者（claude-code / web-admin）
    ReviewedBy  string     `gorm:""`                            // 审批人
    ReviewNote  string     `gorm:"type:text"`                   // 审批备注或拒绝原因
    ExecutedAt  *time.Time `gorm:""`                            // 执行时间（approve 成功后填）
    Error       string     `gorm:"type:text"`                   // 执行失败的错误信息
    CreatedAt   time.Time
    UpdatedAt   time.Time
}
```

**状态流转：**
```
pending → approved   (执行成功)
        → rejected   (人工拒绝)
        → failed     (执行报错)
```

---

## API 设计

所有端点挂在 paas-engine `/api/paas/ops/` 下，Dashboard 通过 `$PAAS_API` 反向代理访问。

| 方法 | 路径 | 调用方 | 说明 |
|------|------|--------|------|
| `POST` | `/api/paas/ops/mutations` | Claude | 提交 DDL/DML 申请 |
| `GET` | `/api/paas/ops/mutations` | Dashboard | 列表查询，支持 `?status=pending` 过滤 |
| `GET` | `/api/paas/ops/mutations/:id` | Dashboard / Claude | 查单条状态 |
| `POST` | `/api/paas/ops/mutations/:id/approve` | Dashboard（人工） | 审批通过，立即执行 SQL |
| `POST` | `/api/paas/ops/mutations/:id/reject` | Dashboard（人工） | 拒绝申请 |

**提交请求体：**
```json
{
  "db": "chiwei",
  "sql": "ALTER TABLE messages ADD COLUMN foo TEXT",
  "reason": "支持新字段 foo 用于存储 xxx"
}
```

**approve 请求体：**
```json
{ "note": "确认可以执行" }
```

**reject 请求体：**
```json
{ "note": "SQL 有误，应加 DEFAULT 约束" }
```

**approve 执行逻辑：**
- 使用写连接（非只读连接）执行 SQL
- 成功：状态 → `approved`，填写 `executed_at`
- 失败：状态 → `failed`，填写 `error`

---

## ops-db Skill 扩展

在现有 `query.py` 基础上新增两个命令：

### 提交变更 `submit`

```
submit @chiwei
ALTER TABLE messages ADD COLUMN foo TEXT;
-- reason: 支持新字段 foo 存储 xxx
```

- `@数据库` 前缀指定目标库（必填）
- `-- reason:` 注释作为说明（建议填写）
- 提交后返回 `mutation_id` 和 `pending` 状态
- Claude 告知用户："已提交审批，ID=42，请前往 Dashboard → DB 变更 页面审批"
- **不做客户端 SQL 类型校验**，写操作是目的，由审批流把关

### 查询状态 `status`

```
status 42
```

返回该条记录的当前状态、审批备注、执行时间或错误信息。

### 数据库选择指引（给 Claude 的说明）

| 库名 | 用途 | 何时使用 |
|------|------|---------|
| `paas_engine` | PaaS 元数据（应用/构建/发布/配置） | 操作 apps、builds、releases、config_bundles 等表 |
| `chiwei` | 业务数据（消息/会话/用户/Agent 数据） | 操作 messages、chats、users、memory 等表 |

提交前先用 `@数据库 schema` 确认表结构和目标库正确。

---

## Dashboard 前端

新增页面 `DbMutations.tsx`，菜单项"DB 变更"，路由 `/db-mutations`。

**页面结构：**
- 顶部 Tab：`待审批` / `已通过` / `已拒绝` / `执行失败`
- 列表列：ID、数据库、SQL 摘要（前 80 字符）、提交人、提交时间、状态
- 点击行展开：完整 SQL、reason、审批备注、执行时间/错误信息
- 待审批行操作：**通过** 按钮 + **拒绝** 按钮
  - 通过：弹出确认 Modal，展示完整 SQL，二次确认后调 approve API
  - 拒绝：弹出 Modal，填写拒绝原因后调 reject API

**安全约束：**
- approve/reject 仅限已登录的 web-admin 用户
- Claude 不能自批自执行（提交和审批是两个不同的 API Token）

---

## 文件改动范围

| 文件 | 改动 |
|------|------|
| `apps/paas-engine/internal/adapter/repository/model.go` | 新增 `DbMutationModel` |
| `apps/paas-engine/internal/adapter/repository/db.go` | AutoMigrate 加入 `DbMutationModel` |
| `apps/paas-engine/internal/adapter/repository/ops_db.go` | 新增写连接 `OpenWriteDB()` |
| `apps/paas-engine/internal/adapter/repository/mutation_repo.go` | 新增 mutation CRUD |
| `apps/paas-engine/internal/adapter/http/ops_handler.go` | 新增 5 个 mutation 端点 |
| `apps/paas-engine/internal/adapter/http/router.go` | 注册新路由 |
| `apps/monitor-dashboard-web/src/pages/DbMutations.tsx` | 新增审批页面 |
| `apps/monitor-dashboard-web/src/App.tsx` | 注册路由和菜单 |
| `.claude/skills/ops-db/SKILL.md` | 更新使用文档（submit/status/库选择指引） |
| `.claude/skills/ops-db/query.py` | 新增 submit/status 命令实现 |
