package http

import (
	"context"
	"crypto/subtle"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

const maxRequestBodySize = 1 << 20 // 1MB

func authMiddleware(token string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			if token == "" {
				next.ServeHTTP(w, r)
				return
			}
			provided := r.Header.Get("X-API-Key")
			if subtle.ConstantTimeCompare([]byte(provided), []byte(token)) != 1 {
				writeJSON(w, http.StatusUnauthorized, map[string]string{"error": "unauthorized"})
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

func loggingMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		rw := &responseWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(rw, r)
		slog.Info("request",
			"method", r.Method,
			"path", r.URL.Path,
			"status", rw.status,
			"duration", time.Since(start).String(),
		)
	})
}

func bodySizeLimitMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Body = http.MaxBytesReader(w, r.Body, maxRequestBodySize)
		next.ServeHTTP(w, r)
	})
}

type responseWriter struct {
	http.ResponseWriter
	status int
}

func (rw *responseWriter) WriteHeader(status int) {
	rw.status = status
	rw.ResponseWriter.WriteHeader(status)
}

type ctxHeadersKey struct{}

func contextPropagationMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		headers := make(map[string]string)
		for key, values := range r.Header {
			lk := strings.ToLower(key)
			if strings.HasPrefix(lk, "x-ctx-") && len(values) > 0 {
				headers[lk] = values[0]
			}
		}
		ctx := context.WithValue(r.Context(), ctxHeadersKey{}, headers)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// GetContextHeaders retrieves x-ctx-* headers stored by contextPropagationMiddleware.
func GetContextHeaders(ctx context.Context) map[string]string {
	headers, _ := ctx.Value(ctxHeadersKey{}).(map[string]string)
	if headers == nil {
		return make(map[string]string)
	}
	return headers
}
