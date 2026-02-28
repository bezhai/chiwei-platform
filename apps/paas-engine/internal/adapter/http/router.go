package http

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
)

func NewRouter(
	appH *AppHandler,
	buildH *BuildHandler,
	releaseH *ReleaseHandler,
	laneH *LaneHandler,
	logH *LogHandler,
	imageRepoH *ImageRepoHandler,
	apiToken string,
) http.Handler {
	r := chi.NewRouter()
	r.Use(middleware.Recoverer)
	r.Use(loggingMiddleware)
	r.Use(bodySizeLimitMiddleware)

	r.Get("/healthz", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
	})

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
			})
		})

		// Releases
		r.Route("/releases", func(r chi.Router) {
			r.Post("/", releaseH.Create)
			r.Get("/", releaseH.List)
			r.Delete("/", releaseH.DeleteByAppAndLane)
			r.Route("/{id}", func(r chi.Router) {
				r.Get("/", releaseH.Get)
				r.Put("/", releaseH.Update)
				r.Delete("/", releaseH.Delete)
			})
		})

		// Lanes
		r.Route("/lanes", func(r chi.Router) {
			r.Post("/", laneH.Create)
			r.Get("/", laneH.List)
			r.Route("/{lane}", func(r chi.Router) {
				r.Get("/", laneH.Get)
				r.Delete("/", laneH.Delete)
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

				// Builds (under image-repos)
				r.Route("/builds", func(r chi.Router) {
					r.Post("/", buildH.Create)
					r.Get("/", buildH.List)
					r.Get("/latest", buildH.GetLatest)
					r.Route("/{id}", func(r chi.Router) {
						r.Get("/", buildH.Get)
						r.Post("/cancel", buildH.Cancel)
						r.Get("/logs", buildH.GetLogs)
					})
				})
			})
		})
	})

	return r
}
