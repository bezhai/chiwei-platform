package http

import (
	"encoding/json"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type BuildHandler struct {
	svc *service.BuildService
}

func NewBuildHandler(svc *service.BuildService) *BuildHandler {
	return &BuildHandler{svc: svc}
}

func (h *BuildHandler) Create(w http.ResponseWriter, r *http.Request) {
	repoName := chi.URLParam(r, "repo")
	var req service.CreateBuildRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	build, err := h.svc.CreateBuild(r.Context(), repoName, req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, build)
}

func (h *BuildHandler) List(w http.ResponseWriter, r *http.Request) {
	repoName := chi.URLParam(r, "repo")
	builds, err := h.svc.ListBuilds(r.Context(), repoName)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, builds)
}

func (h *BuildHandler) Get(w http.ResponseWriter, r *http.Request) {
	repoName := chi.URLParam(r, "repo")
	id := chi.URLParam(r, "id")
	build, err := h.svc.GetBuild(r.Context(), repoName, id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, build)
}

func (h *BuildHandler) Cancel(w http.ResponseWriter, r *http.Request) {
	repoName := chi.URLParam(r, "repo")
	id := chi.URLParam(r, "id")
	if err := h.svc.CancelBuild(r.Context(), repoName, id); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"cancelled": id})
}

func (h *BuildHandler) GetLogs(w http.ResponseWriter, r *http.Request) {
	repoName := chi.URLParam(r, "repo")
	id := chi.URLParam(r, "id")
	logs, err := h.svc.GetBuildLogs(r.Context(), repoName, id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"logs": logs})
}
