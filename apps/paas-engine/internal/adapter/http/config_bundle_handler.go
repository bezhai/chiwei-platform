package http

import (
	"encoding/json"
	"io"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type ConfigBundleHandler struct {
	svc *service.ConfigBundleService
}

func NewConfigBundleHandler(svc *service.ConfigBundleService) *ConfigBundleHandler {
	return &ConfigBundleHandler{svc: svc}
}

func (h *ConfigBundleHandler) Create(w http.ResponseWriter, r *http.Request) {
	var req service.CreateBundleRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	bundle, err := h.svc.CreateBundle(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, bundle)
}

func (h *ConfigBundleHandler) List(w http.ResponseWriter, r *http.Request) {
	bundles, err := h.svc.ListBundles(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundles)
}

func (h *ConfigBundleHandler) Get(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	bundle, err := h.svc.GetBundle(r.Context(), name)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) Update(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, err)
		return
	}
	bundle, err := h.svc.UpdateBundle(r.Context(), name, body)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) Delete(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	if err := h.svc.DeleteBundle(r.Context(), name); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": name})
}

func (h *ConfigBundleHandler) SetKeys(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, err)
		return
	}
	bundle, err := h.svc.SetKeys(r.Context(), name, body)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) DeleteKey(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	key := chi.URLParam(r, "key")
	bundle, err := h.svc.DeleteKey(r.Context(), name, key)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) GenerateKey(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	key := chi.URLParam(r, "key")

	var length int
	body, err := io.ReadAll(r.Body)
	if err == nil && len(body) > 0 {
		var req struct {
			Length int `json:"length"`
		}
		if err := json.Unmarshal(body, &req); err == nil {
			length = req.Length
		}
	}
	if length <= 0 {
		length = 32
	}

	bundle, err := h.svc.GenerateKey(r.Context(), name, key, length)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) SetLaneOverrides(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	lane := chi.URLParam(r, "lane")
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, err)
		return
	}
	bundle, err := h.svc.SetLaneOverrides(r.Context(), name, lane, body)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) DeleteLaneOverrides(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	lane := chi.URLParam(r, "lane")
	bundle, err := h.svc.DeleteLaneOverrides(r.Context(), name, lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) DeleteLaneOverrideKey(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	lane := chi.URLParam(r, "lane")
	key := chi.URLParam(r, "key")
	bundle, err := h.svc.DeleteLaneOverrideKey(r.Context(), name, lane, key)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) ResolveConfig(w http.ResponseWriter, r *http.Request) {
	appName := chi.URLParam(r, "app")
	lane := r.URL.Query().Get("lane")
	config, err := h.svc.ResolveConfig(r.Context(), appName, lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, config)
}
