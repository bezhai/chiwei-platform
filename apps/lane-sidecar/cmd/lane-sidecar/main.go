package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/chiwei-platform/lane-sidecar/internal/iptables"
	"github.com/chiwei-platform/lane-sidecar/internal/proxy"
	"github.com/chiwei-platform/lane-sidecar/internal/registry"
)

func main() {
	initMode := flag.Bool("init", false, "run iptables setup and exit (init container mode)")
	proxyPort := flag.Int("port", 15001, "proxy listen port")
	healthPort := flag.Int("health-port", 15021, "health check port")
	registryURL := flag.String("registry-url", envOrDefault("REGISTRY_URL", "http://lite-registry:8080"), "lite-registry URL")
	pollInterval := flag.Duration("poll-interval", 30*time.Second, "registry poll interval")
	// As a native sidecar, lane-sidecar gets SIGTERM only after the app has
	// exited, so its tunnels are already closing and the cap rarely matters.
	// As a plain container it gets SIGTERM together with the app, and has to
	// keep the app's open connections working through the app's own graceful
	// shutdown (agent-service waits up to 20s) yet still exit by itself
	// before the kubelet's SIGKILL at the default 30s grace period.
	drainTimeout := flag.Duration("drain-timeout", 25*time.Second, "after SIGTERM, how long open connections get to finish; keep it under the pod's termination grace period")
	flag.Parse()

	if *initMode {
		log.Println("[init] setting up iptables rules")
		if err := iptables.Setup(*proxyPort, iptables.ProxyUID); err != nil {
			log.Fatalf("[init] iptables setup failed: %v", err)
		}
		log.Println("[init] iptables rules applied successfully")
		return
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	reg := registry.NewClient(*registryURL, *pollInterval)
	defer reg.Stop()

	srv := proxy.NewServer(fmt.Sprintf(":%d", *proxyPort), reg)

	healthSrv := &http.Server{
		Addr:    fmt.Sprintf(":%d", *healthPort),
		Handler: health(srv),
	}
	go healthSrv.ListenAndServe()

	log.Printf("[proxy] starting on :%d, health on :%d", *proxyPort, *healthPort)
	serveErr := make(chan error, 1)
	go func() { serveErr <- srv.ListenAndServe() }()
	select {
	case err := <-serveErr:
		log.Fatalf("[proxy] server error: %v", err)
	case <-ctx.Done():
	}
	stop() // a second signal kills the process the usual way

	log.Printf("[proxy] shutting down; open connections get up to %s to finish", *drainTimeout)
	shutdownCtx, cancel := context.WithTimeout(context.Background(), *drainTimeout)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("[proxy] closed the connections still open after %s: %v", *drainTimeout, err)
	}
	healthSrv.Shutdown(shutdownCtx)
	log.Println("[proxy] stopped")
}

// health answers the kubelet's probes: ok only while the proxy is serving.
// The app container starts once this sidecar's startup probe passes, and
// every connection it opens is redirected to the proxy, so ok must not come
// before the proxy's listener is bound; it stops once shutdown begins.
func health(srv *proxy.Server) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !srv.Serving() {
			http.Error(w, "proxy not serving", http.StatusServiceUnavailable)
			return
		}
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("ok"))
	})
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
