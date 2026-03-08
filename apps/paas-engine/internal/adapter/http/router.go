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

	r.Route("/api/v1", func(r chi.Router) {
		r.Use(authMiddleware(apiToken))
		// Apps
		r.Route("/apps", func(r chi.Router) {
			r.Post("/", appH.Create)
			r.Get("/", appH.List)
			r.Route("/{app}", func(r chi.Router) {
				r.Get("/", appH.Get)
				r.Put("/", appH.Update)
				r.Delete("/", appH.Delete)
				r.Get("/logs", logH.GetLogs)

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
	})

	return r
}
