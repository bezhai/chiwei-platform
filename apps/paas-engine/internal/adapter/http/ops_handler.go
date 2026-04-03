package http

import (
	"context"
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
	Create(ctx context.Context, m *repository.DbMutationModel) error
	List(ctx context.Context, status string) ([]repository.DbMutationModel, error)
	Get(ctx context.Context, id uint) (*repository.DbMutationModel, error)
	UpdateStatus(ctx context.Context, id uint, status, reviewedBy, reviewNote string, executedAt *time.Time, execErr string) error
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
	if err := h.store.Create(r.Context(), m); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusCreated, m)
}

// ListMutations 返回 db_mutations 列表，支持 ?status=pending 过滤。
func (h *OpsHandler) ListMutations(w http.ResponseWriter, r *http.Request) {
	status := r.URL.Query().Get("status")
	mutations, err := h.store.List(r.Context(), status)
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
	m, err := h.store.Get(r.Context(), id)
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

type reviewRequest struct {
	Note string `json:"note"`
}

// ApproveMutation 审批通过：立即执行 SQL，成功→approved，失败→failed。
func (h *OpsHandler) ApproveMutation(w http.ResponseWriter, r *http.Request) {
	id, err := parseMutationID(w, r)
	if err != nil {
		return
	}
	m, err := h.store.Get(r.Context(), id)
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

	writeDB, ok := h.writeDbs[m.DB]
	if !ok {
		writeJSON(w, http.StatusBadRequest, map[string]string{
			"error": fmt.Sprintf("write database %q not available", m.DB),
		})
		return
	}

	now := time.Now()
	var execErr string
	if result := writeDB.WithContext(r.Context()).Exec(m.SQL); result.Error != nil {
		execErr = result.Error.Error()
	}

	newStatus := "approved"
	var executedAt *time.Time
	if execErr != "" {
		newStatus = "failed"
	} else {
		executedAt = &now
	}

	if err := h.store.UpdateStatus(r.Context(), id, newStatus, "web-admin", req.Note, executedAt, execErr); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	updated, err := h.store.Get(r.Context(), id)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to fetch updated record"})
		return
	}
	writeJSON(w, http.StatusOK, updated)
}

// RejectMutation 拒绝申请，填写原因。
func (h *OpsHandler) RejectMutation(w http.ResponseWriter, r *http.Request) {
	id, err := parseMutationID(w, r)
	if err != nil {
		return
	}
	m, err := h.store.Get(r.Context(), id)
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

	if err := h.store.UpdateStatus(r.Context(), id, "rejected", "web-admin", req.Note, nil, ""); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
		return
	}

	updated, err := h.store.Get(r.Context(), id)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "failed to fetch updated record"})
		return
	}
	writeJSON(w, http.StatusOK, updated)
}
