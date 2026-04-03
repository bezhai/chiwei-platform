package http

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

func NewRouter(
	appH *AppHandler,
	releaseH *ReleaseHandler,
	logH *LogHandler,
	imageRepoH *ImageRepoHandler,
	opsH *OpsHandler,
	pipelineH *PipelineHandler,
	configBundleH *ConfigBundleHandler,
	apiToken string,
) http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(metricsMiddleware)
	r.Use(loggingMiddleware)
	r.Use(bodySizeLimitMiddleware)

	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})
	r.Handle("/metrics", promhttp.Handler())

	r.Route("/api/paas", func(r chi.Router) {
		r.Use(authMiddleware(apiToken))

		// Logs (通用查询)
		r.Get("/logs", logH.QueryLogs)

		// Apps
		r.Route("/apps", func(r chi.Router) {
			r.Post("/", appH.Create)
			r.Get("/", appH.List)
			r.Route("/{app}", func(r chi.Router) {
				r.Get("/", appH.Get)
				r.Put("/", appH.Update)
				r.Delete("/", appH.Delete)
				r.Get("/logs", logH.GetLogs)
				r.Get("/resolved-config", configBundleH.ResolveConfig)

				// Builds (under apps)
				r.Route("/builds", func(r chi.Router) {
					r.Post("/", appH.CreateBuild)
					r.Get("/", appH.ListBuilds)
					r.Get("/latest", appH.GetLatestBuild)
					r.Route("/{id}", func(r chi.Router) {
						r.Get("/", appH.GetBuild)
						r.Post("/cancel", appH.CancelBuild)
						r.Get("/logs", appH.GetBuildLogs)
					})
				})
			})
		})

		// Releases
		r.Route("/releases", func(r chi.Router) {
			r.Post("/", releaseH.Create)
			r.Get("/", releaseH.List)
			r.Delete("/", releaseH.DeleteByAppAndLane)
			r.Get("/orphans", releaseH.GetOrphans)
			r.Delete("/orphans", releaseH.CleanupOrphans)
			r.Route("/{id}", func(r chi.Router) {
				r.Get("/", releaseH.Get)
				r.Put("/", releaseH.Update)
				r.Delete("/", releaseH.Delete)
				r.Get("/status", releaseH.GetStatus)
			})
		})

		// Ops
		r.Route("/ops", func(r chi.Router) {
			r.Post("/query", opsH.Query)
			r.Post("/mutations", opsH.SubmitMutation)
			r.Get("/mutations", opsH.ListMutations)
			r.Route("/mutations/{id}", func(r chi.Router) {
				r.Get("/", opsH.GetMutation)
				r.Post("/approve", opsH.ApproveMutation)
				r.Post("/reject", opsH.RejectMutation)
			})
		})

		// Image Repos
		r.Route("/image-repos", func(r chi.Router) {
			r.Post("/", imageRepoH.Create)
			r.Get("/", imageRepoH.List)
			r.Route("/{repo}", func(r chi.Router) {
				r.Get("/", imageRepoH.Get)
				r.Put("/", imageRepoH.Update)
				r.Delete("/", imageRepoH.Delete)
			})
		})

		// CI Pipeline
		r.Route("/ci", func(r chi.Router) {
			r.Post("/register", pipelineH.Register)
			r.Get("/", pipelineH.List)
			r.Route("/runs/{id}", func(r chi.Router) {
				r.Get("/", pipelineH.GetRun)
				r.Post("/cancel", pipelineH.CancelRun)
				r.Get("/logs", pipelineH.GetLogs)
			})
			r.Route("/{lane}", func(r chi.Router) {
				r.Delete("/", pipelineH.Unregister)
				r.Get("/runs", pipelineH.ListRuns)
				r.Post("/trigger", pipelineH.Trigger)
			})
		})

		// Config Bundles
		r.Route("/config-bundles", func(r chi.Router) {
			r.Post("/", configBundleH.Create)
			r.Get("/", configBundleH.List)
			r.Route("/{bundle}", func(r chi.Router) {
				r.Get("/", configBundleH.Get)
				r.Put("/", configBundleH.Update)
				r.Delete("/", configBundleH.Delete)
				r.Put("/keys", configBundleH.SetKeys)
				r.Delete("/keys/{key}", configBundleH.DeleteKey)
				r.Post("/keys/{key}/generate", configBundleH.GenerateKey)
				r.Route("/lanes/{lane}", func(r chi.Router) {
					r.Put("/", configBundleH.SetLaneOverrides)
					r.Delete("/", configBundleH.DeleteLaneOverrides)
					r.Delete("/{key}", configBundleH.DeleteLaneOverrideKey)
				})
			})
		})
	})

	return r
}
