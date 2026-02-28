package http

import (
	"encoding/json"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type ReleaseHandler struct {
	svc *service.ReleaseService
}

func NewReleaseHandler(svc *service.ReleaseService) *ReleaseHandler {
	return &ReleaseHandler{svc: svc}
}

func (h *ReleaseHandler) Create(w http.ResponseWriter, r *http.Request) {
	var req service.CreateReleaseRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	release, err := h.svc.CreateOrUpdateRelease(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, release)
}

func (h *ReleaseHandler) List(w http.ResponseWriter, r *http.Request) {
	appName := r.URL.Query().Get("app")
	lane := r.URL.Query().Get("lane")
	releases, err := h.svc.ListReleases(r.Context(), appName, lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, releases)
}

func (h *ReleaseHandler) Get(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	release, err := h.svc.GetRelease(r.Context(), id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, release)
}

func (h *ReleaseHandler) Update(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	var req service.CreateReleaseRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	release, err := h.svc.UpdateRelease(r.Context(), id, req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, release)
}

func (h *ReleaseHandler) Delete(w http.ResponseWriter, r *http.Request) {
	id := chi.URLParam(r, "id")
	if err := h.svc.DeleteRelease(r.Context(), id); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": id})
}

func (h *ReleaseHandler) DeleteByAppAndLane(w http.ResponseWriter, r *http.Request) {
	appName := r.URL.Query().Get("app")
	lane := r.URL.Query().Get("lane")
	if appName == "" || lane == "" {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "app and lane query params required"})
		return
	}
	if err := h.svc.DeleteReleaseByAppAndLane(r.Context(), appName, lane); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": appName + "/" + lane})
}
