package http

import (
	"encoding/json"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type GatewayRuleHandler struct {
	svc *service.GatewayRuleService
}

func NewGatewayRuleHandler(svc *service.GatewayRuleService) *GatewayRuleHandler {
	return &GatewayRuleHandler{svc: svc}
}

// List 返回全部规则（管理面，{data:[...]} 信封）。
func (h *GatewayRuleHandler) List(w http.ResponseWriter, r *http.Request) {
	rules, err := h.svc.List(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rules)
}

// Get 返回单条规则（管理面）。
func (h *GatewayRuleHandler) Get(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "name")
	rule, err := h.svc.Get(r.Context(), name)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rule)
}

// Upsert 校验并写入规则（name 来自 URL path 做幂等 key）。
func (h *GatewayRuleHandler) Upsert(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "name")
	var req service.UpsertGatewayRuleRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, domain.ErrInvalidInput)
		return
	}
	if err := h.svc.Upsert(r.Context(), name, req); err != nil {
		writeError(w, err)
		return
	}
	rule, err := h.svc.Get(r.Context(), name)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, rule)
}

// Delete 删除规则。
func (h *GatewayRuleHandler) Delete(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "name")
	if err := h.svc.Delete(r.Context(), name); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": name})
}

// Snapshot 返回完整快照（内部端点，不鉴权，不走信封）。
// api-gateway 直接消费这个 flat JSON：{version, updated_at, rules}。
func (h *GatewayRuleHandler) Snapshot(w http.ResponseWriter, r *http.Request) {
	snap, err := h.svc.Snapshot(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(snap)
}
