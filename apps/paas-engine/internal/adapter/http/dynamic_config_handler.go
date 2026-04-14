package http

import (
	"encoding/json"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type DynamicConfigHandler struct {
	svc *service.DynamicConfigService
}

func NewDynamicConfigHandler(svc *service.DynamicConfigService) *DynamicConfigHandler {
	return &DynamicConfigHandler{svc: svc}
}

func (h *DynamicConfigHandler) Resolve(w http.ResponseWriter, r *http.Request) {
	lane := r.URL.Query().Get("lane")
	result, err := h.svc.Resolve(r.Context(), lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func (h *DynamicConfigHandler) List(w http.ResponseWriter, r *http.Request) {
	lane := r.URL.Query().Get("lane")
	configs, err := h.svc.List(r.Context(), lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, configs)
}

func (h *DynamicConfigHandler) Set(w http.ResponseWriter, r *http.Request) {
	key := chi.URLParam(r, "key")
	var req service.SetDynamicConfigRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	if err := h.svc.Set(r.Context(), key, req); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"key": key, "lane": req.Lane, "status": "ok"})
}

func (h *DynamicConfigHandler) Delete(w http.ResponseWriter, r *http.Request) {
	key := chi.URLParam(r, "key")
	lane := r.URL.Query().Get("lane")
	if err := h.svc.Delete(r.Context(), key, lane); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": key})
}
