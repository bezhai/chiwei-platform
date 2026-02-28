package main

import (
	"context"
	"log"
	"net/http"
	"os/signal"
	"syscall"
	"time"

	"github.com/chiwei-platform/lite-registry/internal/config"
	"github.com/chiwei-platform/lite-registry/internal/handler"
	"github.com/chiwei-platform/lite-registry/internal/registry"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
)

func main() {
	cfg := config.Load()

	clientset, err := newClientset(cfg.KubeconfigPath)
	if err != nil {
		log.Fatalf("failed to create k8s client: %v", err)
	}

	reg := registry.New(clientset, cfg.Namespace, time.Duration(cfg.ResyncSeconds)*time.Second)

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	go func() {
		if err := reg.Start(ctx); err != nil && ctx.Err() == nil {
			log.Fatalf("registry failed: %v", err)
		}
	}()

	// Wait for cache sync before serving traffic
	for !reg.Ready() {
		select {
		case <-ctx.Done():
			log.Fatal("shutdown before registry became ready")
		case <-time.After(100 * time.Millisecond):
		}
	}

	router := handler.NewRouter(reg)
	srv := &http.Server{
		Addr:    ":" + cfg.HTTPPort,
		Handler: router,
	}

	go func() {
		log.Printf("lite-registry listening on :%s", cfg.HTTPPort)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("http server error: %v", err)
		}
	}()

	<-ctx.Done()
	log.Println("shutting down...")

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := srv.Shutdown(shutdownCtx); err != nil {
		log.Printf("http server shutdown error: %v", err)
	}
}

func newClientset(kubeconfigPath string) (kubernetes.Interface, error) {
	var cfg *rest.Config
	var err error

	if kubeconfigPath != "" {
		cfg, err = clientcmd.BuildConfigFromFlags("", kubeconfigPath)
	} else {
		cfg, err = rest.InClusterConfig()
	}
	if err != nil {
		return nil, err
	}

	return kubernetes.NewForConfig(cfg)
}
