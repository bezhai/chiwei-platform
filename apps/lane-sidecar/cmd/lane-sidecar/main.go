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
	flag.Parse()

	if *initMode {
		log.Println("[init] setting up iptables rules")
		if err := iptables.Setup(*proxyPort, iptables.ProxyUID); err != nil {
			log.Fatalf("[init] iptables setup failed: %v", err)
		}
		log.Println("[init] iptables rules applied successfully")
		return
	}

	reg := registry.NewClient(*registryURL, *pollInterval)
	defer reg.Stop()

	srv := proxy.NewServer(fmt.Sprintf(":%d", *proxyPort), reg)

	healthSrv := &http.Server{
		Addr: fmt.Sprintf(":%d", *healthPort),
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(http.StatusOK)
			w.Write([]byte("ok"))
		}),
	}
	go healthSrv.ListenAndServe()

	go func() {
		sigCh := make(chan os.Signal, 1)
		signal.Notify(sigCh, syscall.SIGTERM, syscall.SIGINT)
		<-sigCh
		log.Println("[proxy] shutting down...")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		srv.Shutdown(ctx)
		healthSrv.Shutdown(ctx)
	}()

	log.Printf("[proxy] starting on :%d, health on :%d", *proxyPort, *healthPort)
	if err := srv.ListenAndServe(); err != http.ErrServerClosed {
		log.Fatalf("[proxy] server error: %v", err)
	}
}

func envOrDefault(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
