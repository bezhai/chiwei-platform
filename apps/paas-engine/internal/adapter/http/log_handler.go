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

// QueryLogs 通用日志查询端点 GET /api/v1/logs
func (h *LogHandler) QueryLogs(w http.ResponseWriter, r *http.Request) {
	opts := parseLogQueryOptions(r)

	logs, err := h.svc.QueryLogs(r.Context(), opts)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"logs": logs})
}

// GetLogs 向后兼容端点 GET /api/v1/apps/{app}/logs
func (h *LogHandler) GetLogs(w http.ResponseWriter, r *http.Request) {
	appName := chi.URLParam(r, "app")

	opts := parseLogQueryOptions(r)
	opts.App = appName

	logs, err := h.svc.QueryLogs(r.Context(), opts)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"logs": logs})
}

func parseLogQueryOptions(r *http.Request) service.LogQueryOptions {
	q := r.URL.Query()

	limit := 1000
	if raw := q.Get("limit"); raw != "" {
		if v, err := strconv.Atoi(raw); err == nil {
			limit = v
		}
	}

	since := q.Get("since")
	if since == "" && q.Get("start") == "" {
		since = "1h"
	}

	return service.LogQueryOptions{
		App:       q.Get("app"),
		Lane:      q.Get("lane"),
		Pod:       q.Get("pod"),
		Since:     since,
		Start:     q.Get("start"),
		End:       q.Get("end"),
		Limit:     limit,
		Keyword:   q.Get("keyword"),
		Exclude:   q.Get("exclude"),
		Regexp:    q.Get("regexp"),
		Direction: q.Get("direction"),
	}
}
