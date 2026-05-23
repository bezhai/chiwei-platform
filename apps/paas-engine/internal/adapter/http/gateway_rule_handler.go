package http

import (
	"encoding/json"
	"fmt"
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

// explainRequest 是 POST /api/paas/gateway-rules:explain 的请求体。
type explainRequest struct {
	Path  string `json:"path"`
	XLane string `json:"x_lane"`
}

// Explain 预览一个请求会命中哪条规则、为何命中、其余规则为何没命中。
func (h *GatewayRuleHandler) Explain(w http.ResponseWriter, r *http.Request) {
	var req explainRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, domain.ErrInvalidInput)
		return
	}
	if req.Path == "" {
		writeError(w, fmt.Errorf("%w: path is required", domain.ErrInvalidInput))
		return
	}
	res, err := h.svc.Explain(r.Context(), req.Path, req.XLane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// reasonRequest 是 disable/enable 端点的请求体（仅含 reason，供审计）。
type reasonRequest struct {
	Reason string `json:"reason"`
}

// Disable 把规则 enabled 置 false，返回 before/after 供审计。
func (h *GatewayRuleHandler) Disable(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "name")
	var req reasonRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, domain.ErrInvalidInput)
		return
	}
	res, err := h.svc.Disable(r.Context(), name, req.Reason)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// Enable 把规则 enabled 置 true，返回 before/after 供审计。
func (h *GatewayRuleHandler) Enable(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "name")
	var req reasonRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, domain.ErrInvalidInput)
		return
	}
	res, err := h.svc.Enable(r.Context(), name, req.Reason)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
}

// SetWeights 整体替换规则全部 target 权重，返回 before/after 供审计。
func (h *GatewayRuleHandler) SetWeights(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "name")
	var req service.SetWeightsRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, domain.ErrInvalidInput)
		return
	}
	res, err := h.svc.SetWeights(r.Context(), name, req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, res)
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
