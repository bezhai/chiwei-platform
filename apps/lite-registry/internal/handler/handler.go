package handler

import (
	"encoding/json"
	"log"
	"net/http"
	"time"

	"github.com/chiwei-platform/lite-registry/internal/registry"
	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
)

type Handler struct {
	reg *registry.Registry
}

func NewRouter(reg *registry.Registry) http.Handler {
	h := &Handler{reg: reg}

	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(loggingMiddleware)

	r.Get("/healthz", h.healthz)
	r.Get("/readyz", h.readyz)

	r.Route("/v1", func(r chi.Router) {
		r.Get("/routes", h.listRoutes)
		r.Get("/routes/{service}", h.getRoute)
	})

	return r
}

type RoutesResponse struct {
	Services  map[string]registry.ServiceInfo `json:"services"`
	UpdatedAt string                          `json:"updated_at"`
}

func (h *Handler) healthz(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *Handler) readyz(w http.ResponseWriter, r *http.Request) {
	if !h.reg.Ready() {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "not ready"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (h *Handler) listRoutes(w http.ResponseWriter, r *http.Request) {
	snap := h.reg.Snapshot()
	writeJSON(w, http.StatusOK, RoutesResponse{
		Services:  snap,
		UpdatedAt: h.reg.UpdatedAt().Format(time.RFC3339),
	})
}

func (h *Handler) getRoute(w http.ResponseWriter, r *http.Request) {
	service := chi.URLParam(r, "service")
	info, ok := h.reg.Get(service)
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "service not found"})
		return
	}
	writeJSON(w, http.StatusOK, info)
}

func writeJSON(w http.ResponseWriter, status int, v interface{}) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(v); err != nil {
		log.Printf("handler: failed to encode response: %v", err)
	}
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
		next.ServeHTTP(ww, r)
		log.Printf("%s %s %d %s", r.Method, r.URL.Path, ww.Status(), time.Since(start))
	})
}
