package main

import (
	"context"
	"log"
	"log/slog"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/config"
	"github.com/chiwei-platform/api-gateway/internal/gateway"
	"github.com/chiwei-platform/api-gateway/internal/middleware"
	"github.com/chiwei-platform/api-gateway/internal/registry"
	"github.com/chiwei-platform/api-gateway/internal/route"
)

func main() {
	cfg := config.Load()

	// Load routes from YAML, fallback to hardcoded routes
	routes, err := route.LoadFromFile(cfg.RoutesConfig)
	if err != nil {
		slog.Warn("failed to load routes config, using fallback", "path", cfg.RoutesConfig, "error", err)
		routes = route.FallbackRoutes()
	}
	slog.Info("routes loaded", "count", len(routes))

	matcher := route.NewMatcher(routes)

	// Start registry client (polls lite-registry in background)
	reg := registry.NewClient(cfg.RegistryURL, time.Duration(cfg.PollIntervalSeconds)*time.Second)

	// Build gateway handler
	gw := gateway.New(matcher, reg, time.Duration(cfg.ProxyTimeoutSeconds)*time.Second)

	// Build HTTP mux with health checks
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})
	mux.HandleFunc("/readyz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})
	mux.Handle("/", gw)

	// Apply middleware chain
	handler := middleware.Chain(
		middleware.Recovery,
		middleware.RequestID,
		middleware.Logging,
	)(mux)

	srv := &http.Server{
		Addr:    ":" + cfg.HTTPPort,
		Handler: handler,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		slog.Info("api-gateway listening", "port", cfg.HTTPPort)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server error: %v", err)
		}
	}()

	<-ctx.Done()
	slog.Info("shutting down...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		slog.Error("http server shutdown error", "error", err)
	}
}
