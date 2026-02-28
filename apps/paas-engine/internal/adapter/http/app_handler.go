package http

import (
	"encoding/json"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type AppHandler struct {
	svc      *service.AppService
	buildSvc *service.BuildService
}

func NewAppHandler(svc *service.AppService, buildSvc *service.BuildService) *AppHandler {
	return &AppHandler{svc: svc, buildSvc: buildSvc}
}

func (h *AppHandler) Create(w http.ResponseWriter, r *http.Request) {
	var req service.CreateAppRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	app, err := h.svc.CreateApp(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, app)
}

func (h *AppHandler) List(w http.ResponseWriter, r *http.Request) {
	apps, err := h.svc.ListApps(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, apps)
}

func (h *AppHandler) Get(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "app")
	app, err := h.svc.GetApp(r.Context(), name)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, app)
}

func (h *AppHandler) Update(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "app")
	var req service.UpdateAppRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	app, err := h.svc.UpdateApp(r.Context(), name, req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, app)
}

func (h *AppHandler) Delete(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "app")
	if err := h.svc.DeleteApp(r.Context(), name); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": name})
}

// getImageRepoName resolves app name to its ImageRepoName.
func (h *AppHandler) getImageRepoName(w http.ResponseWriter, r *http.Request) (string, bool) {
	appName := chi.URLParam(r, "app")
	app, err := h.svc.GetApp(r.Context(), appName)
	if err != nil {
		writeError(w, err)
		return "", false
	}
	return app.ImageRepoName, true
}

func (h *AppHandler) CreateBuild(w http.ResponseWriter, r *http.Request) {
	repoName, ok := h.getImageRepoName(w, r)
	if !ok {
		return
	}
	var req service.CreateBuildRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	build, err := h.buildSvc.CreateBuild(r.Context(), repoName, req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, build)
}

func (h *AppHandler) ListBuilds(w http.ResponseWriter, r *http.Request) {
	repoName, ok := h.getImageRepoName(w, r)
	if !ok {
		return
	}
	builds, err := h.buildSvc.ListBuilds(r.Context(), repoName)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, builds)
}

func (h *AppHandler) GetLatestBuild(w http.ResponseWriter, r *http.Request) {
	repoName, ok := h.getImageRepoName(w, r)
	if !ok {
		return
	}
	build, err := h.buildSvc.GetLatestSuccessfulBuild(r.Context(), repoName)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, build)
}

func (h *AppHandler) GetBuild(w http.ResponseWriter, r *http.Request) {
	repoName, ok := h.getImageRepoName(w, r)
	if !ok {
		return
	}
	id := chi.URLParam(r, "id")
	build, err := h.buildSvc.GetBuild(r.Context(), repoName, id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, build)
}

func (h *AppHandler) CancelBuild(w http.ResponseWriter, r *http.Request) {
	repoName, ok := h.getImageRepoName(w, r)
	if !ok {
		return
	}
	id := chi.URLParam(r, "id")
	if err := h.buildSvc.CancelBuild(r.Context(), repoName, id); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"cancelled": id})
}

func (h *AppHandler) GetBuildLogs(w http.ResponseWriter, r *http.Request) {
	repoName, ok := h.getImageRepoName(w, r)
	if !ok {
		return
	}
	id := chi.URLParam(r, "id")
	logs, err := h.buildSvc.GetBuildLogs(r.Context(), repoName, id)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"logs": logs})
}
