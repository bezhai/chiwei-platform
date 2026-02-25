package http

import (
	"net/http"
	"strconv"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type LogHandler struct {
	svc *service.LogService
}

func NewLogHandler(svc *service.LogService) *LogHandler {
	return &LogHandler{svc: svc}
}

func (h *LogHandler) GetLogs(w http.ResponseWriter, r *http.Request) {
	appName := chi.URLParam(r, "app")

	lane := r.URL.Query().Get("lane")

	since := r.URL.Query().Get("since")
	if since == "" {
		since = "1h"
	}

	limit := 1000
	if raw := r.URL.Query().Get("limit"); raw != "" {
		if v, err := strconv.Atoi(raw); err == nil {
			limit = v
		}
	}

	logs, err := h.svc.GetAppLogs(r.Context(), appName, lane, since, limit)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"logs": logs})
}
