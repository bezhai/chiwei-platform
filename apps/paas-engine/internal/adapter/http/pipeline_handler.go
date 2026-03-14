package http

import (
	"encoding/json"
	"net/http"
	"strconv"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type PipelineHandler struct {
	svc *service.PipelineService
}

func NewPipelineHandler(svc *service.PipelineService) *PipelineHandler {
	return &PipelineHandler{svc: svc}
}

// Register 注册 CI 泳道。
// POST /api/paas/ci/register
func (h *PipelineHandler) Register(w http.ResponseWriter, r *http.Request) {
	var req service.RegisterCIRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	cfg, err := h.svc.RegisterCI(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, cfg)
}

// Unregister 注销 CI 泳道。
// DELETE /api/paas/ci/{lane}
func (h *PipelineHandler) Unregister(w http.ResponseWriter, r *http.Request) {
	lane := chi.URLParam(r, "lane")
	if err := h.svc.UnregisterCI(r.Context(), lane); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"archived": lane})
}

// List 列出所有 CI 配置。
// GET /api/paas/ci
func (h *PipelineHandler) List(w http.ResponseWriter, r *http.Request) {
	configs, err := h.svc.ListCIConfigs(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, configs)
}

// ListRuns 列出泳道的 pipeline 执行记录。
// GET /api/paas/ci/{lane}/runs
func (h *PipelineHandler) ListRuns(w http.ResponseWriter, r *http.Request) {
	lane := chi.URLParam(r, "lane")
	limit := 10
	if l := r.URL.Query().Get("limit"); l != "" {
		if n, err := strconv.Atoi(l); err == nil && n > 0 {
			limit = n
		}
	}
	runs, err := h.svc.ListPipelineRuns(r.Context(), lane, limit)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, runs)
}

// GetRun 获取 pipeline run 详情。
// GET /api/paas/ci/runs/{id}
func (h *PipelineHandler) GetRun(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	run, err := h.svc.GetPipelineRun(r.Context(), id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, run)
}

// CancelRun 取消 pipeline run。
// POST /api/paas/ci/runs/{id}/cancel
func (h *PipelineHandler) CancelRun(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	if err := h.svc.CancelPipelineRun(r.Context(), id); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"cancelled": id})
}

// GetLogs 获取 job 日志。
// GET /api/paas/ci/runs/{id}/logs
func (h *PipelineHandler) GetLogs(w http.ResponseWriter, r *http.Request) {
	jobID := r.URL.Query().Get("job")
	if jobID == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "job query param required"})
		return
	}
	logs, err := h.svc.GetJobLogs(r.Context(), jobID)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"logs": logs})
}

// Trigger 手动触发 pipeline。
// POST /api/paas/ci/{lane}/trigger
func (h *PipelineHandler) Trigger(w http.ResponseWriter, r *http.Request) {
	lane := chi.URLParam(r, "lane")
	var req service.TriggerPipelineRequest
	if r.Body != nil {
		_ = json.NewDecoder(r.Body).Decode(&req)
	}
	run, err := h.svc.TriggerPipeline(r.Context(), lane, req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, run)
}
