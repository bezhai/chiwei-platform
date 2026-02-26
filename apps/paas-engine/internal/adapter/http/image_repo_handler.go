package http

import (
	"encoding/json"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type ImageRepoHandler struct {
	svc *service.ImageRepoService
}

func NewImageRepoHandler(svc *service.ImageRepoService) *ImageRepoHandler {
	return &ImageRepoHandler{svc: svc}
}

func (h *ImageRepoHandler) Create(w http.ResponseWriter, r *http.Request) {
	var req service.CreateImageRepoRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	repo, err := h.svc.CreateImageRepo(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, repo)
}

func (h *ImageRepoHandler) List(w http.ResponseWriter, r *http.Request) {
	repos, err := h.svc.ListImageRepos(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, repos)
}

func (h *ImageRepoHandler) Get(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "repo")
	repo, err := h.svc.GetImageRepo(r.Context(), name)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, repo)
}

func (h *ImageRepoHandler) Update(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "repo")
	var req service.UpdateImageRepoRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	repo, err := h.svc.UpdateImageRepo(r.Context(), name, req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, repo)
}

func (h *ImageRepoHandler) Delete(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "repo")
	if err := h.svc.DeleteImageRepo(r.Context(), name); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": name})
}
