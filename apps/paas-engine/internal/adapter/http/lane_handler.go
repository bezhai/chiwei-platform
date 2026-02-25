package http

import (
	"encoding/json"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type LaneHandler struct {
	svc *service.LaneService
}

func NewLaneHandler(svc *service.LaneService) *LaneHandler {
	return &LaneHandler{svc: svc}
}

func (h *LaneHandler) Create(w http.ResponseWriter, r *http.Request) {
	var req service.CreateLaneRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	lane, err := h.svc.CreateLane(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, lane)
}

func (h *LaneHandler) List(w http.ResponseWriter, r *http.Request) {
	lanes, err := h.svc.ListLanes(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, lanes)
}

func (h *LaneHandler) Get(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "lane")
	lane, err := h.svc.GetLane(r.Context(), name)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, lane)
}

func (h *LaneHandler) Delete(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "lane")
	if err := h.svc.DeleteLane(r.Context(), name); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": name})
}
