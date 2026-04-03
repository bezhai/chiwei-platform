# DDL/DML Human-Approval Mutation Support — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 Claude 通过 `ops-db` skill 提交 DDL/DML 申请，人工在 Dashboard 审批后由 paas-engine 立即执行。

**Architecture:** paas-engine 新增 `db_mutations` 表存储申请记录，审批通过后用写连接执行 SQL。monitor-dashboard 作为代理层转发请求。前端新增审批页面。ops-db skill 新增 `submit` / `status` 命令。

**Tech Stack:** Go + GORM + chi (paas-engine), TypeScript + Koa (monitor-dashboard), React + Ant Design (monitor-dashboard-web), Python (ops-db skill)

---

## 文件改动范围

| 文件 | 操作 | 说明 |
|------|------|------|
| `apps/paas-engine/internal/adapter/repository/model.go` | Modify | 新增 `DbMutationModel` |
| `apps/paas-engine/internal/adapter/repository/db.go` | Modify | AutoMigrate 加入 `DbMutationModel` |
| `apps/paas-engine/internal/adapter/repository/ops_db.go` | Modify | 新增 `OpenWriteDB()` |
| `apps/paas-engine/internal/adapter/repository/mutation_repo.go` | Create | `MutationRepo` 实现 |
| `apps/paas-engine/internal/adapter/http/ops_handler.go` | Modify | 新增 `MutationStore` 接口 + 5个处理方法 |
| `apps/paas-engine/internal/adapter/http/mutation_handler_test.go` | Create | handler 单元测试 |
| `apps/paas-engine/internal/adapter/http/router.go` | Modify | 注册新路由 |
| `apps/paas-engine/cmd/paas-engine/main.go` | Modify | 注入写连接 + MutationRepo |
| `apps/monitor-dashboard/src/routes/operations.ts` | Modify | 新增 mutations 代理路由 |
| `apps/monitor-dashboard-web/src/pages/DbMutations.tsx` | Create | 审批页面 |
| `apps/monitor-dashboard-web/src/App.tsx` | Modify | 注册路由和菜单项 |
| `.claude/skills/ops-db/query.py` | Modify | 新增 `submit` / `status` 命令 |
| `.claude/skills/ops-db/SKILL.md` | Modify | 更新文档 |

---

## Task 1: 新增 DbMutationModel + OpenWriteDB

**Files:**
- Modify: `apps/paas-engine/internal/adapter/repository/model.go`
- Modify: `apps/paas-engine/internal/adapter/repository/db.go`
- Modify: `apps/paas-engine/internal/adapter/repository/ops_db.go`

- [ ] **Step 1: 在 model.go 末尾追加 DbMutationModel**

在 `apps/paas-engine/internal/adapter/repository/model.go` 文件末尾（第 142 行后）添加：

```go
// DbMutationModel 记录一条待审批的 DDL/DML 申请。
type DbMutationModel struct {
	ID          uint       `gorm:"primaryKey;autoIncrement"`
	DB          string     `gorm:"not null"`                   // 目标库: paas_engine / chiwei
	SQL         string     `gorm:"not null;type:text"`         // 提交的 SQL
	Reason      string     `gorm:"type:text"`                  // 提交说明
	Status      string     `gorm:"not null;default:'pending'"` // pending/approved/rejected/failed
	SubmittedBy string     `gorm:"not null"`                   // 提交者（claude-code / web-admin）
	ReviewedBy  string     ``                                  // 审批人
	ReviewNote  string     `gorm:"type:text"`                  // 审批备注或拒绝原因
	ExecutedAt  *time.Time ``                                  // 执行时间（approve 成功后填写）
	Error       string     `gorm:"type:text"`                  // 执行失败的错误信息
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

func (DbMutationModel) TableName() string { return "db_mutations" }
```

- [ ] **Step 2: 在 db.go 的 AutoMigrate 调用中加入 DbMutationModel**

修改 `apps/paas-engine/internal/adapter/repository/db.go` 中的 `AutoMigrate` 调用：

```go
	if err := db.AutoMigrate(
		&AppModel{},
		&ImageRepoModel{},
		&BuildModel{},
		&ReleaseModel{},
		&CIConfigModel{},
		&PipelineRunModel{},
		&StageRunModel{},
		&JobRunModel{},
		&ConfigBundleModel{},
		&DbMutationModel{},
	); err != nil {
		return nil, err
	}
```

- [ ] **Step 3: 在 ops_db.go 末尾追加 OpenWriteDB**

在 `apps/paas-engine/internal/adapter/repository/ops_db.go` 末尾添加：

```go
// OpenWriteDB opens a database connection with write access (no AutoMigrate).
// Used for executing approved DDL/DML mutations on external databases.
func OpenWriteDB(dsn string) (*gorm.DB, error) {
	return gorm.Open(postgres.Open(dsn), &gorm.Config{
		Logger: logger.Default.LogMode(logger.Warn),
	})
}
```

- [ ] **Step 4: 编译验证**

```bash
cd apps/paas-engine && make build
```

预期：编译成功，无报错。

- [ ] **Step 5: 运行测试**

```bash
cd apps/paas-engine && make test
```

预期：所有已有测试通过。

- [ ] **Step 6: 提交**

```bash
git add apps/paas-engine/internal/adapter/repository/model.go \
        apps/paas-engine/internal/adapter/repository/db.go \
        apps/paas-engine/internal/adapter/repository/ops_db.go
git commit -m "feat(paas-engine): add DbMutationModel and OpenWriteDB"
```

---

## Task 2: 新增 MutationRepo

**Files:**
- Create: `apps/paas-engine/internal/adapter/repository/mutation_repo.go`

- [ ] **Step 1: 创建 mutation_repo.go**

新建文件 `apps/paas-engine/internal/adapter/repository/mutation_repo.go`，内容如下：

```go
package repository

import (
	"time"

	"gorm.io/gorm"
)

// MutationRepo 实现对 db_mutations 表的增删改查。
type MutationRepo struct {
	db *gorm.DB
}

func NewMutationRepo(db *gorm.DB) *MutationRepo {
	return &MutationRepo{db: db}
}

func (r *MutationRepo) Create(m *DbMutationModel) error {
	return r.db.Create(m).Error
}

// List 返回 db_mutations 记录，按 created_at 降序。status 为空时返回全部。
func (r *MutationRepo) List(status string) ([]DbMutationModel, error) {
	q := r.db.Order("created_at DESC")
	if status != "" {
		q = q.Where("status = ?", status)
	}
	var result []DbMutationModel
	return result, q.Find(&result).Error
}

func (r *MutationRepo) Get(id uint) (*DbMutationModel, error) {
	var m DbMutationModel
	err := r.db.First(&m, id).Error
	return &m, err
}

// UpdateStatus 更新审批结果相关字段。
func (r *MutationRepo) UpdateStatus(id uint, status, reviewedBy, reviewNote string, executedAt *time.Time, execErr string) error {
	updates := map[string]interface{}{
		"status":      status,
		"reviewed_by": reviewedBy,
		"review_note": reviewNote,
		"executed_at": executedAt,
		"error":       execErr,
	}
	return r.db.Model(&DbMutationModel{}).Where("id = ?", id).Updates(updates).Error
}
```

- [ ] **Step 2: 编译验证**

```bash
cd apps/paas-engine && make build
```

预期：编译成功。

- [ ] **Step 3: 提交**

```bash
git add apps/paas-engine/internal/adapter/repository/mutation_repo.go
git commit -m "feat(paas-engine): add MutationRepo for db_mutations CRUD"
```

---

## Task 3: 新增 mutation HTTP 处理器（submit + list + get）

**Files:**
- Modify: `apps/paas-engine/internal/adapter/http/ops_handler.go`
- Create: `apps/paas-engine/internal/adapter/http/mutation_handler_test.go`

- [ ] **Step 1: 新建测试文件，写三个失败测试**

新建 `apps/paas-engine/internal/adapter/http/mutation_handler_test.go`：

```go
package http

import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/adapter/repository"
	"github.com/go-chi/chi/v5"
)

// fakeMutationStore 是 MutationStore 的内存实现，用于单元测试。
type fakeMutationStore struct {
	mutations map[uint]*repository.DbMutationModel
	nextID    uint
}

func newFakeMutationStore() *fakeMutationStore {
	return &fakeMutationStore{
		mutations: make(map[uint]*repository.DbMutationModel),
		nextID:    1,
	}
}

func (f *fakeMutationStore) Create(m *repository.DbMutationModel) error {
	m.ID = f.nextID
	f.nextID++
	cp := *m
	f.mutations[cp.ID] = &cp
	return nil
}

func (f *fakeMutationStore) List(status string) ([]repository.DbMutationModel, error) {
	var result []repository.DbMutationModel
	for _, m := range f.mutations {
		if status == "" || m.Status == status {
			result = append(result, *m)
		}
	}
	return result, nil
}

func (f *fakeMutationStore) Get(id uint) (*repository.DbMutationModel, error) {
	m, ok := f.mutations[id]
	if !ok {
		return nil, errors.New("record not found")
	}
	cp := *m
	return &cp, nil
}

func (f *fakeMutationStore) UpdateStatus(id uint, status, reviewedBy, reviewNote string, executedAt *time.Time, execErr string) error {
	m, ok := f.mutations[id]
	if !ok {
		return errors.New("record not found")
	}
	m.Status = status
	m.ReviewedBy = reviewedBy
	m.ReviewNote = reviewNote
	m.ExecutedAt = executedAt
	m.Error = execErr
	return nil
}

func TestSubmitMutation_MissingSQL(t *testing.T) {
	h := NewOpsHandler(nil, nil, newFakeMutationStore())
	r := chi.NewRouter()
	r.Post("/mutations", h.SubmitMutation)

	body, _ := json.Marshal(map[string]string{"db": "chiwei"})
	req := httptest.NewRequest(http.MethodPost, "/mutations", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Errorf("want 400, got %d", rec.Code)
	}
}

func TestListMutations_FilterByStatus(t *testing.T) {
	store := newFakeMutationStore()
	_ = store.Create(&repository.DbMutationModel{DB: "chiwei", SQL: "SELECT 1", Status: "pending", SubmittedBy: "claude-code"})
	_ = store.Create(&repository.DbMutationModel{DB: "chiwei", SQL: "SELECT 2", Status: "approved", SubmittedBy: "claude-code"})

	h := NewOpsHandler(nil, nil, store)
	r := chi.NewRouter()
	r.Get("/mutations", h.ListMutations)

	req := httptest.NewRequest(http.MethodGet, "/mutations?status=pending", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("want 200, got %d", rec.Code)
	}

	var resp []map[string]interface{}
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	if len(resp) != 1 {
		t.Errorf("want 1 pending mutation, got %d", len(resp))
	}
}

func TestGetMutation_NotFound(t *testing.T) {
	h := NewOpsHandler(nil, nil, newFakeMutationStore())
	r := chi.NewRouter()
	r.Get("/mutations/{id}", h.GetMutation)

	req := httptest.NewRequest(http.MethodGet, "/mutations/999", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("want 404, got %d", rec.Code)
	}
}
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd apps/paas-engine && go test ./internal/adapter/http/... -run "TestSubmitMutation|TestListMutations|TestGetMutation" -v
```

预期：编译失败（`SubmitMutation`、`ListMutations`、`GetMutation` 方法不存在）。

- [ ] **Step 3: 更新 ops_handler.go，重写结构体和新增三个方法**

用以下内容**完整替换** `apps/paas-engine/internal/adapter/http/ops_handler.go`：

```go
package http

import (
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/adapter/repository"
	"github.com/go-chi/chi/v5"
	"gorm.io/gorm"
)

var writeKeywordRE = regexp.MustCompile(
	`(?i)\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b`,
)

// MutationStore 是 db_mutations 的存储接口（消费方定义）。
type MutationStore interface {
	Create(m *repository.DbMutationModel) error
	List(status string) ([]repository.DbMutationModel, error)
	Get(id uint) (*repository.DbMutationModel, error)
	UpdateStatus(id uint, status, reviewedBy, reviewNote string, executedAt *time.Time, execErr string) error
}

type OpsHandler struct {
	dbs      map[string]*gorm.DB // alias → 只读连接（用于查询）
	writeDbs map[string]*gorm.DB // alias → 写连接（用于执行 mutation）
	store    MutationStore
}

func NewOpsHandler(dbs map[string]*gorm.DB, writeDbs map[string]*gorm.DB, store MutationStore) *OpsHandler {
	return &OpsHandler{dbs: dbs, writeDbs: writeDbs, store: store}
}

// ── 只读查询（原有逻辑） ────────────────────────────────────────────────────

type opsQueryRequest struct {
	DB  string `json:"db"`
	SQL string `json:"sql"`
}

type opsQueryResponse struct {
	Columns []string `json:"columns"`
	Rows    [][]any  `json:"rows"`
}

func (h *OpsHandler) Query(w http.ResponseWriter, r *http.Request) {
	var req opsQueryRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}

	req.SQL = strings.TrimSpace(req.SQL)
	if req.SQL == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sql is required"})
		return
	}

	if writeKeywordRE.MatchString(req.SQL) {
		writeJSON(w, http.StatusForbidden, map[string]string{"error": "write operations are not allowed"})
		return
	}

	dbAlias := req.DB
	if dbAlias == "" {
		dbAlias = "paas_engine"
	}
	db, ok := h.dbs[dbAlias]
	if !ok {
		available := make([]string, 0, len(h.dbs))
		for k := range h.dbs {
			available = append(available, k)
		}
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": fmt.Sprintf("unknown database %q, available: %s", dbAlias, strings.Join(available, ", ")),
		})
		return
	}

	rows, err := db.WithContext(r.Context()).Raw(req.SQL).Rows()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	defer rows.Close()

	columns, err := rows.Columns()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	var result [][]any
	for rows.Next() {
		values := make([]any, len(columns))
		ptrs := make([]any, len(columns))
		for i := range values {
			ptrs[i] = &values[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
			return
		}
		row := make([]any, len(values))
		for i, v := range values {
			if b, ok := v.([]byte); ok {
				row[i] = string(b)
			} else {
				row[i] = v
			}
		}
		result = append(result, row)
	}

	if err := rows.Err(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	writeJSON(w, http.StatusOK, opsQueryResponse{
		Columns: columns,
		Rows:    result,
	})
}

// ── DDL/DML 审批流 ─────────────────────────────────────────────────────────

type submitMutationRequest struct {
	DB          string `json:"db"`
	SQL         string `json:"sql"`
	Reason      string `json:"reason"`
	SubmittedBy string `json:"submitted_by"`
}

// SubmitMutation 接收 Claude 提交的 DDL/DML 申请，存为 pending 状态。
func (h *OpsHandler) SubmitMutation(w http.ResponseWriter, r *http.Request) {
	var req submitMutationRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON: " + err.Error()})
		return
	}
	req.SQL = strings.TrimSpace(req.SQL)
	if req.SQL == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "sql is required"})
		return
	}
	if req.DB == "" {
		req.DB = "paas_engine"
	}
	if req.SubmittedBy == "" {
		req.SubmittedBy = "unknown"
	}

	m := &repository.DbMutationModel{
		DB:          req.DB,
		SQL:         req.SQL,
		Reason:      req.Reason,
		Status:      "pending",
		SubmittedBy: req.SubmittedBy,
	}
	if err := h.store.Create(m); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, m)
}

// ListMutations 返回 db_mutations 列表，支持 ?status=pending 过滤。
func (h *OpsHandler) ListMutations(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	mutations, err := h.store.List(status)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	if mutations == nil {
		mutations = []repository.DbMutationModel{}
	}
	writeJSON(w, http.StatusOK, mutations)
}

// GetMutation 返回单条 mutation 详情。
func (h *OpsHandler) GetMutation(w http.ResponseWriter, r *http.Request) {
	id, err := parseMutationID(w, r)
	if err != nil {
		return
	}
	m, err := h.store.Get(id)
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "mutation not found"})
		return
	}
	writeJSON(w, http.StatusOK, m)
}

// parseMutationID 从 chi URL 参数解析 mutation ID，失败时写入错误响应并返回 error。
func parseMutationID(w http.ResponseWriter, r *http.Request) (uint, error) {
	idStr := chi.URLParam(r, "id")
	id64, err := strconv.ParseUint(idStr, 10, 64)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid mutation id"})
		return 0, err
	}
	return uint(id64), nil
}
```

- [ ] **Step 4: 运行测试，确认三个测试通过**

```bash
cd apps/paas-engine && go test ./internal/adapter/http/... -run "TestSubmitMutation|TestListMutations|TestGetMutation" -v
```

预期：
```
--- PASS: TestSubmitMutation_MissingSQL
--- PASS: TestListMutations_FilterByStatus
--- PASS: TestGetMutation_NotFound
```

- [ ] **Step 5: 运行全部测试**

```bash
cd apps/paas-engine && make test
```

预期：全部通过。

- [ ] **Step 6: 提交**

```bash
git add apps/paas-engine/internal/adapter/http/ops_handler.go \
        apps/paas-engine/internal/adapter/http/mutation_handler_test.go
git commit -m "feat(paas-engine): add SubmitMutation/ListMutations/GetMutation handlers"
```

---

## Task 4: 新增 approve + reject 处理器

**Files:**
- Modify: `apps/paas-engine/internal/adapter/http/ops_handler.go`
- Modify: `apps/paas-engine/internal/adapter/http/mutation_handler_test.go`

- [ ] **Step 1: 在测试文件追加 approve/reject 测试**

在 `mutation_handler_test.go` 末尾追加：

```go
func TestApproveMutation_NotFound(t *testing.T) {
	h := NewOpsHandler(nil, map[string]*gorm.DB{}, newFakeMutationStore())
	r := chi.NewRouter()
	r.Post("/mutations/{id}/approve", h.ApproveMutation)

	body, _ := json.Marshal(map[string]string{"note": "ok"})
	req := httptest.NewRequest(http.MethodPost, "/mutations/999/approve", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusNotFound {
		t.Errorf("want 404, got %d", rec.Code)
	}
}

func TestApproveMutation_NotPending(t *testing.T) {
	store := newFakeMutationStore()
	_ = store.Create(&repository.DbMutationModel{
		DB: "chiwei", SQL: "DROP TABLE foo", Status: "rejected", SubmittedBy: "claude-code",
	})

	h := NewOpsHandler(nil, map[string]*gorm.DB{}, store)
	r := chi.NewRouter()
	r.Post("/mutations/{id}/approve", h.ApproveMutation)

	body, _ := json.Marshal(map[string]string{"note": "ok"})
	req := httptest.NewRequest(http.MethodPost, "/mutations/1/approve", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusConflict {
		t.Errorf("want 409, got %d", rec.Code)
	}
}

func TestRejectMutation_OK(t *testing.T) {
	store := newFakeMutationStore()
	_ = store.Create(&repository.DbMutationModel{
		DB: "chiwei", SQL: "DROP TABLE foo", Status: "pending", SubmittedBy: "claude-code",
	})

	h := NewOpsHandler(nil, nil, store)
	r := chi.NewRouter()
	r.Post("/mutations/{id}/reject", h.RejectMutation)

	body, _ := json.Marshal(map[string]string{"note": "dangerous"})
	req := httptest.NewRequest(http.MethodPost, "/mutations/1/reject", bytes.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Errorf("want 200, got %d", rec.Code)
	}

	m, _ := store.Get(1)
	if m.Status != "rejected" {
		t.Errorf("want status=rejected, got %s", m.Status)
	}
	if m.ReviewNote != "dangerous" {
		t.Errorf("want note=dangerous, got %s", m.ReviewNote)
	}
}
```

同时在文件顶部 import 块补充 `"gorm.io/gorm"` （TestApproveMutation_NotPending 需要）。完整 import 块：

```go
import (
	"bytes"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/adapter/repository"
	"github.com/go-chi/chi/v5"
	"gorm.io/gorm"
)
```

- [ ] **Step 2: 运行测试，确认失败**

```bash
cd apps/paas-engine && go test ./internal/adapter/http/... -run "TestApproveMutation|TestRejectMutation" -v
```

预期：编译失败（`ApproveMutation`、`RejectMutation` 方法不存在）。

- [ ] **Step 3: 在 ops_handler.go 末尾追加 ApproveMutation 和 RejectMutation**

在 `ops_handler.go` 最后追加以下内容：

```go
type reviewRequest struct {
	Note string `json:"note"`
}

// ApproveMutation 审批通过：立即执行 SQL，成功→approved，失败→failed。
func (h *OpsHandler) ApproveMutation(w http.ResponseWriter, r *http.Request) {
	id, err := parseMutationID(w, r)
	if err != nil {
		return
	}
	m, err := h.store.Get(id)
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "mutation not found"})
		return
	}
	if m.Status != "pending" {
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": fmt.Sprintf("mutation is already %s", m.Status),
		})
		return
	}

	var req reviewRequest
	_ = json.NewDecoder(r.Body).Decode(&req)

	// 获取写连接
	writeDB, ok := h.writeDbs[m.DB]
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": fmt.Sprintf("write database %q not available", m.DB),
		})
		return
	}

	// 执行 SQL
	now := time.Now()
	var execErr string
	if result := writeDB.WithContext(r.Context()).Exec(m.SQL); result.Error != nil {
		execErr = result.Error.Error()
	}

	newStatus := "approved"
	if execErr != "" {
		newStatus = "failed"
	}

	if err := h.store.UpdateStatus(id, newStatus, "web-admin", req.Note, &now, execErr); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	updated, _ := h.store.Get(id)
	writeJSON(w, http.StatusOK, updated)
}

// RejectMutation 拒绝申请，填写原因。
func (h *OpsHandler) RejectMutation(w http.ResponseWriter, r *http.Request) {
	id, err := parseMutationID(w, r)
	if err != nil {
		return
	}
	m, err := h.store.Get(id)
	if err != nil {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "mutation not found"})
		return
	}
	if m.Status != "pending" {
		writeJSON(w, http.StatusConflict, map[string]string{
			"error": fmt.Sprintf("mutation is already %s", m.Status),
		})
		return
	}

	var req reviewRequest
	_ = json.NewDecoder(r.Body).Decode(&req)

	if err := h.store.UpdateStatus(id, "rejected", "web-admin", req.Note, nil, ""); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	updated, _ := h.store.Get(id)
	writeJSON(w, http.StatusOK, updated)
}
```

- [ ] **Step 4: 运行 approve/reject 测试，确认通过**

```bash
cd apps/paas-engine && go test ./internal/adapter/http/... -run "TestApproveMutation|TestRejectMutation" -v
```

预期：
```
--- PASS: TestApproveMutation_NotFound
--- PASS: TestApproveMutation_NotPending
--- PASS: TestRejectMutation_OK
```

- [ ] **Step 5: 运行全部测试**

```bash
cd apps/paas-engine && make test
```

预期：全部通过。

- [ ] **Step 6: 提交**

```bash
git add apps/paas-engine/internal/adapter/http/ops_handler.go \
        apps/paas-engine/internal/adapter/http/mutation_handler_test.go
git commit -m "feat(paas-engine): add ApproveMutation/RejectMutation handlers"
```

---

## Task 5: 注册路由并接入 main.go

**Files:**
- Modify: `apps/paas-engine/internal/adapter/http/router.go`
- Modify: `apps/paas-engine/cmd/paas-engine/main.go`

- [ ] **Step 1: 在 router.go 的 ops 路由块中注册新端点**

将 `router.go` 第 79-81 行的 ops 路由块替换为：

```go
		// Ops
		r.Route("/ops", func(r chi.Router) {
			r.Post("/query", opsH.Query)
			r.Post("/mutations", opsH.SubmitMutation)
			r.Get("/mutations", opsH.ListMutations)
			r.Route("/mutations/{id}", func(r chi.Router) {
				r.Get("/", opsH.GetMutation)
				r.Post("/approve", opsH.ApproveMutation)
				r.Post("/reject", opsH.RejectMutation)
			})
		})
```

- [ ] **Step 2: 更新 main.go，创建写连接和 MutationRepo**

在 `main.go` 中找到 `// Ops 数据库连接池（只读查询）` 注释块（第 112 行），将其替换为：

```go
	// Ops 数据库连接池（只读查询）
	opsDbs := map[string]*gorm.DB{"paas_engine": db}
	writeDbs := map[string]*gorm.DB{"paas_engine": db}
	if cfg.ChiweiDatabaseURL != "" {
		chiweiReadDB, err := repository.OpenReadOnlyDB(cfg.ChiweiDatabaseURL)
		if err != nil {
			slog.Warn("chiwei read database unavailable for ops queries", "error", err)
		} else {
			opsDbs["chiwei"] = chiweiReadDB
		}

		chiweiWriteDB, err := repository.OpenWriteDB(cfg.ChiweiDatabaseURL)
		if err != nil {
			slog.Warn("chiwei write database unavailable for ops mutations", "error", err)
		} else {
			writeDbs["chiwei"] = chiweiWriteDB
		}
	}
	mutationRepo := repository.NewMutationRepo(db)
```

同时将 `httpadapter.NewOpsHandler(opsDbs)` 调用（第 129 行附近）更新为：

```go
		httpadapter.NewOpsHandler(opsDbs, writeDbs, mutationRepo),
```

- [ ] **Step 3: 编译验证**

```bash
cd apps/paas-engine && make build
```

预期：编译成功。

- [ ] **Step 4: 运行全部测试**

```bash
cd apps/paas-engine && make test
```

预期：全部通过。

- [ ] **Step 5: 提交**

```bash
git add apps/paas-engine/internal/adapter/http/router.go \
        apps/paas-engine/cmd/paas-engine/main.go
git commit -m "feat(paas-engine): wire up mutation routes and write DB connections"
```

---

## Task 6: monitor-dashboard 新增 mutations 代理路由

**Files:**
- Modify: `apps/monitor-dashboard/src/routes/operations.ts`

- [ ] **Step 1: 在 operations.ts 中追加五个代理路由**

在 `operations.ts` 第 65 行（`// ---------- 写操作 ----------` 注释之前）插入以下内容：

```typescript
// ---------- DB Mutations（DDL/DML 审批流） ----------

/** POST /api/ops/db-mutations — Claude 提交 DDL/DML 申请 */
router.post('/api/ops/db-mutations', async (ctx) => {
  const data = await paasClient.post('/api/paas/ops/mutations', ctx.request.body);
  ctx.body = data;
});

/** GET /api/ops/db-mutations — 列表查询，支持 ?status=pending 过滤 */
router.get('/api/ops/db-mutations', async (ctx) => {
  const status = (ctx.query.status as string) || '';
  const data = await paasClient.get('/api/paas/ops/mutations', status ? { status } : undefined);
  ctx.body = data;
});

/** GET /api/ops/db-mutations/:id — 单条详情 */
router.get('/api/ops/db-mutations/:id', async (ctx) => {
  const data = await paasClient.get(`/api/paas/ops/mutations/${ctx.params.id}`);
  ctx.body = data;
});

/** POST /api/ops/db-mutations/:id/approve — 审批通过（立即执行） */
router.post('/api/ops/db-mutations/:id/approve', async (ctx) => {
  const data = await paasClient.post(`/api/paas/ops/mutations/${ctx.params.id}/approve`, ctx.request.body);
  ctx.body = data;
});

/** POST /api/ops/db-mutations/:id/reject — 拒绝申请 */
router.post('/api/ops/db-mutations/:id/reject', async (ctx) => {
  const data = await paasClient.post(`/api/paas/ops/mutations/${ctx.params.id}/reject`, ctx.request.body);
  ctx.body = data;
});
```

- [ ] **Step 2: 编译验证**

```bash
cd apps/monitor-dashboard && npm run build
```

预期：编译成功。

- [ ] **Step 3: 提交**

```bash
git add apps/monitor-dashboard/src/routes/operations.ts
git commit -m "feat(monitor-dashboard): proxy mutation routes to paas-engine"
```

---

## Task 7: 新增 DbMutations 前端审批页面

**Files:**
- Create: `apps/monitor-dashboard-web/src/pages/DbMutations.tsx`

- [ ] **Step 1: 创建 DbMutations.tsx**

新建 `apps/monitor-dashboard-web/src/pages/DbMutations.tsx`：

```tsx
import { useEffect, useState } from 'react';
import {
  Button,
  Input,
  Modal,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { CheckOutlined, CloseOutlined } from '@ant-design/icons';
import { api } from '../api/client';
import dayjs from 'dayjs';

const { Text, Paragraph } = Typography;
const { TextArea } = Input;

interface DbMutation {
  id: number;
  db: string;
  sql: string;
  reason: string;
  status: 'pending' | 'approved' | 'rejected' | 'failed';
  submitted_by: string;
  reviewed_by: string;
  review_note: string;
  executed_at: string | null;
  error: string;
  created_at: string;
}

const STATUS_TABS = [
  { key: 'pending', label: '待审批' },
  { key: 'approved', label: '已通过' },
  { key: 'rejected', label: '已拒绝' },
  { key: 'failed', label: '执行失败' },
];

const STATUS_COLOR: Record<string, string> = {
  pending: 'orange',
  approved: 'green',
  rejected: 'default',
  failed: 'red',
};

export default function DbMutations() {
  const [activeTab, setActiveTab] = useState('pending');
  const [mutations, setMutations] = useState<DbMutation[]>([]);
  const [loading, setLoading] = useState(false);
  const [rejectTarget, setRejectTarget] = useState<DbMutation | null>(null);
  const [rejectNote, setRejectNote] = useState('');
  const [approveTarget, setApproveTarget] = useState<DbMutation | null>(null);

  const fetchMutations = async (status: string) => {
    setLoading(true);
    try {
      const { data } = await api.get(`/ops/db-mutations?status=${status}`);
      setMutations(data || []);
    } catch {
      message.error('加载失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchMutations(activeTab);
  }, [activeTab]);

  const handleApprove = async () => {
    if (!approveTarget) return;
    try {
      await api.post(`/ops/db-mutations/${approveTarget.id}/approve`, { note: '' });
      message.success('已审批通过，SQL 执行完毕');
      setApproveTarget(null);
      fetchMutations(activeTab);
    } catch (e: unknown) {
      const err = e as { response?: { data?: { message?: string } } };
      message.error(err?.response?.data?.message || '执行失败');
    }
  };

  const handleReject = async () => {
    if (!rejectTarget) return;
    try {
      await api.post(`/ops/db-mutations/${rejectTarget.id}/reject`, { note: rejectNote });
      message.success('已拒绝');
      setRejectTarget(null);
      setRejectNote('');
      fetchMutations(activeTab);
    } catch {
      message.error('操作失败');
    }
  };

  const columns: ColumnsType<DbMutation> = [
    { title: 'ID', dataIndex: 'id', width: 60 },
    {
      title: '数据库',
      dataIndex: 'db',
      width: 120,
      render: (v) => <Tag>{v}</Tag>,
    },
    {
      title: 'SQL',
      dataIndex: 'sql',
      ellipsis: true,
      render: (v) => (
        <Tooltip title={<pre style={{ maxWidth: 500, whiteSpace: 'pre-wrap' }}>{v}</pre>}>
          <Text code style={{ fontSize: 12 }}>
            {v.length > 80 ? v.slice(0, 80) + '…' : v}
          </Text>
        </Tooltip>
      ),
    },
    { title: '提交人', dataIndex: 'submitted_by', width: 110 },
    {
      title: '提交时间',
      dataIndex: 'created_at',
      width: 160,
      render: (v) => dayjs(v).format('MM-DD HH:mm:ss'),
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 90,
      render: (v) => <Tag color={STATUS_COLOR[v]}>{v}</Tag>,
    },
    {
      title: '操作',
      width: 140,
      render: (_, record) =>
        record.status === 'pending' ? (
          <Space>
            <Button
              type="primary"
              size="small"
              icon={<CheckOutlined />}
              onClick={() => setApproveTarget(record)}
            >
              通过
            </Button>
            <Button
              danger
              size="small"
              icon={<CloseOutlined />}
              onClick={() => {
                setRejectTarget(record);
                setRejectNote('');
              }}
            >
              拒绝
            </Button>
          </Space>
        ) : null,
    },
  ];

  return (
    <>
      <Tabs
        activeKey={activeTab}
        onChange={setActiveTab}
        items={STATUS_TABS.map((t) => ({ key: t.key, label: t.label }))}
      />
      <Table
        rowKey="id"
        loading={loading}
        dataSource={mutations}
        columns={columns}
        expandable={{
          expandedRowRender: (record) => (
            <Space direction="vertical" style={{ width: '100%' }}>
              <div>
                <Text strong>完整 SQL：</Text>
                <Paragraph code copyable style={{ marginTop: 4 }}>
                  {record.sql}
                </Paragraph>
              </div>
              {record.reason && (
                <div>
                  <Text strong>说明：</Text>
                  <Text>{record.reason}</Text>
                </div>
              )}
              {record.review_note && (
                <div>
                  <Text strong>审批备注：</Text>
                  <Text>{record.review_note}</Text>
                </div>
              )}
              {record.executed_at && (
                <div>
                  <Text strong>执行时间：</Text>
                  <Text>{dayjs(record.executed_at).format('YYYY-MM-DD HH:mm:ss')}</Text>
                </div>
              )}
              {record.error && (
                <div>
                  <Text strong type="danger">
                    错误：
                  </Text>
                  <Text type="danger">{record.error}</Text>
                </div>
              )}
            </Space>
          ),
        }}
        pagination={{ pageSize: 20 }}
      />

      {/* 通过确认弹窗 */}
      <Modal
        title="确认执行 SQL"
        open={!!approveTarget}
        onOk={handleApprove}
        onCancel={() => setApproveTarget(null)}
        okText="确认执行"
        okType="primary"
        cancelText="取消"
      >
        {approveTarget && (
          <>
            <Text type="warning" strong>
              以下 SQL 将立即在 {approveTarget.db} 上执行：
            </Text>
            <Paragraph code copyable style={{ marginTop: 8 }}>
              {approveTarget.sql}
            </Paragraph>
            {approveTarget.reason && (
              <Text type="secondary">说明：{approveTarget.reason}</Text>
            )}
          </>
        )}
      </Modal>

      {/* 拒绝弹窗 */}
      <Modal
        title="拒绝申请"
        open={!!rejectTarget}
        onOk={handleReject}
        onCancel={() => setRejectTarget(null)}
        okText="确认拒绝"
        okType="danger"
        cancelText="取消"
      >
        <TextArea
          rows={3}
          placeholder="填写拒绝原因（可选）"
          value={rejectNote}
          onChange={(e) => setRejectNote(e.target.value)}
        />
      </Modal>
    </>
  );
}
```

- [ ] **Step 2: 编译验证**

```bash
cd apps/monitor-dashboard-web && npm run build
```

预期：编译成功，无 TypeScript 报错。

- [ ] **Step 3: 提交**

```bash
git add apps/monitor-dashboard-web/src/pages/DbMutations.tsx
git commit -m "feat(dashboard-web): add DbMutations approval page"
```

---

## Task 8: 注册路由和菜单项

**Files:**
- Modify: `apps/monitor-dashboard-web/src/App.tsx`

- [ ] **Step 1: 在 App.tsx 中追加 DbMutations 页面**

**1.1** 在 `App.tsx` 已有的 lazy import 块中（第 35 行之后）追加：

```tsx
const DbMutations = lazy(() => import('./pages/DbMutations'));
```

**1.2** 在 `menuItems` 数组中，找到 `{ key: '/audit-logs', ... }` 那一行（第 55 行），在其后追加菜单项（在同一个 `type: 'divider'` 分组下）：

```tsx
  { key: '/db-mutations', icon: <DatabaseOutlined />, label: 'DB 变更' },
```

完整 menuItems（替换原有第 51-63 行）：

```tsx
const menuItems: MenuItem[] = [
  { key: '/', icon: <DashboardOutlined />, label: '总览' },
  { key: '/activity', icon: <ThunderboltOutlined />, label: '赤尾动态' },
  { key: '/messages', icon: <MessageOutlined />, label: '消息记录' },
  { key: '/audit-logs', icon: <AuditOutlined />, label: '审计日志' },
  { key: '/db-mutations', icon: <DatabaseOutlined />, label: 'DB 变更' },
  { type: 'divider' },
  { key: '/providers', icon: <CloudServerOutlined />, label: '服务商' },
  { key: '/model-mappings', icon: <ApiOutlined />, label: '模型映射' },
  { type: 'divider' },
  { key: '/kibana', icon: <FileSearchOutlined />, label: 'Grafana' },
  { key: '/langfuse', icon: <MonitorOutlined />, label: 'Langfuse 链路' },
  { key: '/mongo', icon: <DatabaseOutlined />, label: 'Mongo 浏览器' },
];
```

**1.3** 在 Routes 块中（第 280 行之后，`<Route path="/mongo"` 前）追加：

```tsx
                  <Route path="/db-mutations" element={<DbMutations />} />
```

- [ ] **Step 2: 编译验证**

```bash
cd apps/monitor-dashboard-web && npm run build
```

预期：编译成功。

- [ ] **Step 3: 提交**

```bash
git add apps/monitor-dashboard-web/src/App.tsx
git commit -m "feat(dashboard-web): register /db-mutations route and menu item"
```

---

## Task 9: 更新 ops-db skill

**Files:**
- Modify: `.claude/skills/ops-db/query.py`
- Modify: `.claude/skills/ops-db/SKILL.md`

- [ ] **Step 1: 更新 query.py，新增 submit 和 status 命令**

用以下内容**完整替换** `.claude/skills/ops-db/query.py`：

```python
#!/usr/bin/env python3
"""ops-db skill query runner — read-only queries and write mutation submission."""

import json
import os
import re
import subprocess
import sys

WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

SCHEMA_SQL = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='public' ORDER BY table_name"
)

DB_ALIASES = {
    "paas-engine": "paas_engine",
    "paas_engine": "paas_engine",
    "chiwei": "chiwei",
}
DEFAULT_DB = "paas_engine"


def get_env():
    paas_api = os.environ.get("PAAS_API", "")
    cc_token = os.environ.get("DASHBOARD_CC_TOKEN", "")
    if not paas_api:
        print("ERROR: PAAS_API 环境变量未设置", file=sys.stderr)
        sys.exit(1)
    return paas_api, cc_token


def curl_post(url, payload, token):
    result = subprocess.run(
        [
            "curl", "-sfS", "-X", "POST", url,
            "-H", "Content-Type: application/json",
            "-H", f"X-API-Key: {token}",
            "-d", json.dumps(payload),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: API 调用失败: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def curl_get(url, token):
    result = subprocess.run(
        [
            "curl", "-sfS", url,
            "-H", f"X-API-Key: {token}",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: API 调用失败: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def cmd_query(args):
    """默认模式：只读 SQL 查询。"""
    dbname = DEFAULT_DB
    if args[0].startswith("@"):
        alias = args.pop(0)[1:]
        if alias not in DB_ALIASES:
            print(f"ERROR: 未知数据库 '{alias}'，可用: {', '.join(sorted(set(DB_ALIASES.values())))}", file=sys.stderr)
            sys.exit(1)
        dbname = DB_ALIASES[alias]
        if not args:
            print("ERROR: 缺少 SQL 查询", file=sys.stderr)
            sys.exit(1)

    sql = " ".join(args).strip()
    if sql.lower() == "schema":
        sql = SCHEMA_SQL

    if WRITE_KEYWORDS.search(sql):
        print(f"ERROR: 拒绝执行写操作（请用 submit 命令提交审批）: {sql}", file=sys.stderr)
        sys.exit(1)

    paas_api, cc_token = get_env()
    resp = curl_post(
        f"{paas_api}/dashboard/api/ops/db-query",
        {"db": dbname, "sql": sql},
        cc_token,
    )
    if "error" in resp and resp["error"]:
        print(f"ERROR: {resp['error']}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(
        {"columns": resp.get("columns", []), "rows": resp.get("rows", [])},
        default=str, ensure_ascii=False,
    ))


def cmd_submit(args):
    """submit @<db> <SQL> [-- reason: <说明>]
    提交 DDL/DML 申请，等待人工在 Dashboard 审批。
    """
    if not args:
        print("用法: submit @<db> <SQL>", file=sys.stderr)
        sys.exit(1)

    dbname = DEFAULT_DB
    if args[0].startswith("@"):
        alias = args.pop(0)[1:]
        if alias not in DB_ALIASES:
            print(f"ERROR: 未知数据库 '{alias}'", file=sys.stderr)
            sys.exit(1)
        dbname = DB_ALIASES[alias]

    raw = " ".join(args).strip()

    # 从注释中提取 reason
    reason = ""
    sql_lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("-- reason:"):
            reason = stripped[len("-- reason:"):].strip()
        else:
            sql_lines.append(line)
    sql = "\n".join(sql_lines).strip()

    if not sql:
        print("ERROR: SQL 为空", file=sys.stderr)
        sys.exit(1)

    paas_api, cc_token = get_env()
    resp = curl_post(
        f"{paas_api}/dashboard/api/ops/db-mutations",
        {"db": dbname, "sql": sql, "reason": reason, "submitted_by": "claude-code"},
        cc_token,
    )
    if "error" in resp and resp.get("error"):
        print(f"ERROR: {resp['error']}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(resp, default=str, ensure_ascii=False))


def cmd_status(args):
    """status <id> — 查询 mutation 审批状态。"""
    if not args:
        print("用法: status <mutation_id>", file=sys.stderr)
        sys.exit(1)

    mutation_id = args[0].strip()
    paas_api, cc_token = get_env()
    resp = curl_get(
        f"{paas_api}/dashboard/api/ops/db-mutations/{mutation_id}",
        cc_token,
    )
    print(json.dumps(resp, default=str, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print("用法: query.py <command> [args...]", file=sys.stderr)
        print("命令: [@db] <SQL|schema>  |  submit @<db> <SQL>  |  status <id>", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    first = args[0].lower()

    if first == "submit":
        cmd_submit(args[1:])
    elif first == "status":
        cmd_status(args[1:])
    else:
        cmd_query(args)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 更新 SKILL.md**

用以下内容**完整替换** `.claude/skills/ops-db/SKILL.md`：

```markdown
---
description: 安全查询 PaaS Engine PostgreSQL 数据库，以及提交 DDL/DML 变更申请
user_invocable: true
---

# /ops-db

查询 PostgreSQL 数据库（只读），或提交 DDL/DML 变更申请（需人工审批后执行）。

## 用法

### 只读查询

```
/ops-db <SQL>                    # 查询 paas_engine（默认）
/ops-db @chiwei <SQL>            # 查询 chiwei
/ops-db @paas-engine <SQL>       # 查询 paas_engine（显式指定）
/ops-db schema                   # 查看 paas_engine 的表结构
/ops-db @chiwei schema           # 查看 chiwei 的表结构
```

### 提交变更申请

```
/ops-db submit @chiwei ALTER TABLE messages ADD COLUMN foo TEXT;
-- reason: 支持新字段 foo 存储 xxx
```

- `@数据库` 必填，指定目标库
- `-- reason:` 说明变更目的（强烈建议填写）
- 提交后返回 `mutation_id`，状态为 `pending`
- **告知用户**：已提交审批，ID=<id>，请前往 Dashboard → DB 变更 页面审批

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
```

- [ ] **Step 3: 验证 query.py 语法**

```bash
python3 -c "import ast; ast.parse(open('.claude/skills/ops-db/query.py').read()); print('syntax OK')"
```

预期：`syntax OK`

- [ ] **Step 4: 提交**

```bash
git add .claude/skills/ops-db/query.py .claude/skills/ops-db/SKILL.md
git commit -m "feat(ops-db): add submit/status commands and update docs"
```

---

## Self-Review

**Spec coverage 检查：**

| Spec 要求 | 覆盖任务 |
|----------|---------|
| Claude 通过 ops-db skill 提交 DDL/DML | Task 9 |
| paas-engine 存储 db_mutations 表 | Task 1-2 |
| 提交 API (POST /mutations) | Task 3 |
| 列表 + 详情 API | Task 3 |
| 审批通过立即执行 SQL | Task 4 |
| 拒绝填写原因 | Task 4 |
| 执行失败置 failed 状态 | Task 4 |
| 写连接对 chiwei 支持 | Task 5 |
| monitor-dashboard 代理路由 | Task 6 |
| Dashboard 审批页面（Tab + 列表 + 操作） | Task 7 |
| 通过时二次确认 Modal 展示完整 SQL | Task 7 |
| 路由和菜单注册 | Task 8 |
| ops-db skill submit 命令 | Task 9 |
| ops-db skill status 命令 | Task 9 |
| 数据库选择指引 | Task 9 (SKILL.md) |

**类型一致性：** `MutationStore` 接口与 `MutationRepo` 方法签名在 Task 2/3 中定义一致。`parseMutationID` 在 Task 3 定义，Task 4 的 approve/reject 复用它。

**无 placeholder：** 所有步骤都有完整代码。
